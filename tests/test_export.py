"""Tests for the static read-only snapshot export (`wishmap export --user`).

Hybrid style: build a self-contained wishmap install in tmp_path (server
TOML + one account whose routes live in its file tree), point
WISHMAP_CONFIG at it, then call `wishmap.export` directly (no HTTP layer).
Asserts the artifact shape, the read-only bake, the absence of
credentials, that only the named account is exported, and that a
re-export reflects new routes.
"""

import json
import re
from pathlib import Path

import pytest

from wishmap import export, pins_store, ratings, routes_store, users
from wishmap.models import PinConfig, RouteConfig
from wishmap.state import user_dir

MINIMAL_GPX = """<?xml version="1.0"?>
<gpx version="1.1" creator="test" xmlns="http://www.topografix.com/GPX/1/1">
  <trk><name>t</name><trkseg>
    <trkpt lat="46.9" lon="9.7"/>
    <trkpt lat="46.91" lon="9.71"/>
  </trkseg></trk>
</gpx>
"""

WEB_GPX = """<?xml version="1.0"?>
<gpx version="1.1" creator="test" xmlns="http://www.topografix.com/GPX/1/1">
  <trk><name>web</name><trkseg>
    <trkpt lat="47.5" lon="8.5"/>
    <trkpt lat="47.51" lon="8.51"/>
  </trkseg></trk>
</gpx>
"""

# The server TOML still carries Garmin + Strava sections (the interim sync
# reads them), so the no-credentials assertion is meaningful: these
# sentinel values must NEVER appear in the export.
GARMIN_USER = "secret-garmin-user@gmail.com"
GARMIN_PASSWORD = "super-secret-garmin-password"
STRAVA_SECRET = "super-secret-strava-client-value"

CONFIG_TOML = (
    'title = "Test Atlas"\n'
    "\n"
    "[garmin]\n"
    f'username = "{GARMIN_USER}"\n'
    f'password = "{GARMIN_PASSWORD}"\n'
    "\n"
    "[strava]\n"
    'client_id = "12345"\n'
    f'client_secret = "{STRAVA_SECRET}"\n'
)

PASSWORD = "correct horse battery"


@pytest.fixture
def install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "wishmap.toml").write_text(CONFIG_TOML)
    monkeypatch.setenv("WISHMAP_CONFIG", str(tmp_path / "wishmap.toml"))
    return tmp_path


def _add_user(install_dir: Path, username: str, data_dir: str = "data") -> int:
    db_path = install_dir / data_dir / "wishmap.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return users.create_user(db_path, username, PASSWORD).id


def _add_route(
    install_dir: Path,
    user_id: int,
    route_id: str,
    data_dir: str = "data",
    gpx_text: str = WEB_GPX,
) -> None:
    """Insert a renderable route (DB row + GPX in the user's tree)."""
    data_root = install_dir / data_dir
    gpx_rel = f"gpx/web/{route_id}.gpx"
    gpx_abs = user_dir(data_root, user_id) / gpx_rel
    gpx_abs.parent.mkdir(parents=True, exist_ok=True)
    gpx_abs.write_text(gpx_text)
    routes_store.insert(
        data_root / "wishmap.db",
        user_id,
        RouteConfig(
            id=route_id,
            name="Web Route",
            sport=["biking"],
            status="idea",  # type: ignore[arg-type]  # str coerced to Status
            gpx=gpx_rel,
        ),
    )


@pytest.fixture
def alice(install: Path) -> int:
    """An account with one route (r1)."""
    uid = _add_user(install, "alice")
    _add_route(install, uid, "r1", gpx_text=MINIMAL_GPX)
    return uid


# ── Artifact shape ────────────────────────────────────────────────────


def test_export_writes_index_and_four_json(alice: int, tmp_path: Path) -> None:
    out = tmp_path / "public"
    written = export.export_site(out, "alice")
    assert (out / "index.html").is_file()
    for name in ("config", "pins", "routes", "ratings"):
        assert (out / "api" / f"{name}.json").is_file()
    # export_site reports exactly the five files it wrote.
    assert len(written) == 5
    assert (out / "index.html") in written


def test_export_config_json_readonly_true(alice: int, tmp_path: Path) -> None:
    out = tmp_path / "public"
    export.export_site(out, "alice")
    cfg = json.loads((out / "api" / "config.json").read_text())
    assert cfg["readonly"] is True
    assert cfg["title"] == "Test Atlas"


def test_export_pins_and_routes_shape(
    install: Path, alice: int, tmp_path: Path
) -> None:
    pins_store.insert(
        install / "data" / "wishmap.db",
        alice,
        PinConfig(
            id="p1",
            name="Hut",
            lat=46.0,
            lon=9.0,
            sport=["hiking"],
            status="idea",  # type: ignore[arg-type]  # str coerced to Status
        ),
    )
    out = tmp_path / "public"
    export.export_site(out, "alice")
    routes = json.loads((out / "api" / "routes.json").read_text())
    assert routes["type"] == "FeatureCollection"
    feat = next(f for f in routes["features"] if f["properties"]["id"] == "r1")
    assert feat["geometry"]["type"] == "LineString"
    # GeoJSON order is [lon, lat]; MINIMAL_GPX's first trkpt is lat=46.9 lon=9.7.
    assert feat["geometry"]["coordinates"][0] == [9.7, 46.9]
    pins = json.loads((out / "api" / "pins.json").read_text())
    ids = {f["properties"]["id"] for f in pins["features"]}
    # The account's pin, plus the route's derived route-start Point.
    assert ids == {"p1", "r1-start"}


def test_export_ratings_json_matches_get_ratings_shape(
    install: Path, alice: int, tmp_path: Path
) -> None:
    # ratings.json must be a dict keyed by route_id (mirroring GET /api/ratings),
    # not a list. Seed one rating and assert the shape.
    ratings.upsert(install / "data" / "wishmap.db", alice, "r1", {"fun": 4})

    out = tmp_path / "public"
    export.export_site(out, "alice")
    by_id = json.loads((out / "api" / "ratings.json").read_text())
    assert isinstance(by_id, dict)
    assert by_id["r1"]["fun"] == 4
    assert by_id["r1"]["difficulty"] is None
    assert by_id["r1"]["updated_at"]


def test_export_is_scoped_to_the_named_account(
    install: Path, alice: int, tmp_path: Path
) -> None:
    bob = _add_user(install, "bob")
    _add_route(install, bob, "bobs-route")
    ratings.upsert(install / "data" / "wishmap.db", bob, "bobs-route", {"fun": 1})

    out = tmp_path / "public"
    export.export_site(out, "ALICE")  # usernames match case-insensitively
    routes = json.loads((out / "api" / "routes.json").read_text())
    assert {f["properties"]["id"] for f in routes["features"]} == {"r1"}
    assert json.loads((out / "api" / "ratings.json").read_text()) == {}


# ── Edge cases ────────────────────────────────────────────────────────


def test_export_unknown_account_writes_nothing(install: Path, tmp_path: Path) -> None:
    # Fresh install: the data dir does not exist yet. The export must mkdir
    # it before opening the DB (sqlite "unable to open" otherwise), then
    # refuse the unknown account without writing a partial artifact.
    assert not (install / "data").exists()
    out = tmp_path / "public"
    with pytest.raises(export.ExportError, match="No account named 'nobody'"):
        export.export_site(out, "nobody")
    assert (install / "data" / "wishmap.db").is_file()
    assert not out.exists()


def test_export_of_an_empty_account(install: Path, tmp_path: Path) -> None:
    _add_user(install, "alice")
    out = tmp_path / "public"
    export.export_site(out, "alice")
    assert json.loads((out / "api" / "ratings.json").read_text()) == {}
    for name in ("pins", "routes"):
        payload = json.loads((out / "api" / f"{name}.json").read_text())
        assert payload == {"type": "FeatureCollection", "features": []}


def test_export_skips_a_route_with_a_missing_gpx(
    install: Path, alice: int, tmp_path: Path
) -> None:
    _add_route(install, alice, "gone")
    (user_dir(install / "data", alice) / "gpx/web/gone.gpx").unlink()
    out = tmp_path / "public"
    export.export_site(out, "alice")
    routes = json.loads((out / "api" / "routes.json").read_text())
    assert {f["properties"]["id"] for f in routes["features"]} == {"r1"}


def test_re_export_reflects_new_route(
    install: Path, alice: int, tmp_path: Path
) -> None:
    out = tmp_path / "public"
    export.export_site(out, "alice")
    routes = json.loads((out / "api" / "routes.json").read_text())
    assert all(
        f["properties"]["id"] != "web-strava-789" for f in routes["features"]
    )

    _add_route(install, alice, "web-strava-789")

    export.export_site(out, "alice")  # re-export over the same dir
    routes = json.loads((out / "api" / "routes.json").read_text())
    ids = {f["properties"]["id"] for f in routes["features"]}
    assert ids == {"r1", "web-strava-789"}
    pins = json.loads((out / "api" / "pins.json").read_text())
    assert any(
        f["properties"]["id"] == "web-strava-789-start" for f in pins["features"]
    )


def test_export_reads_the_db_under_data_dir(install: Path, tmp_path: Path) -> None:
    # The export must read the server's DB and files, wherever `data_dir`
    # puts them.
    toml = install / "wishmap.toml"
    toml.write_text('data_dir = "state"\n' + toml.read_text())
    uid = _add_user(install, "alice", data_dir="state")
    _add_route(install, uid, "web-strava-789", data_dir="state")

    out = tmp_path / "public"
    export.export_site(out, "alice")
    routes = json.loads((out / "api" / "routes.json").read_text())
    assert "web-strava-789" in {f["properties"]["id"] for f in routes["features"]}
    assert not (install / "data").exists()


# ── No credentials in the export ──────────────────────────────────────


def test_export_contains_no_credentials(alice: int, tmp_path: Path) -> None:
    out = tmp_path / "public"
    export.export_site(out, "alice")
    blob = "".join(
        (out / "api" / f"{name}.json").read_text()
        for name in ("config", "pins", "routes", "ratings")
    )
    for sensitive in (
        GARMIN_USER,
        GARMIN_PASSWORD,
        STRAVA_SECRET,
        PASSWORD,
        "@gmail",
        "client_secret",
        "scrypt$",
    ):
        assert sensitive not in blob


# ── Read-only gate baked into the HTML ────────────────────────────────


def test_export_html_readonly_baked(alice: int, tmp_path: Path) -> None:
    out = tmp_path / "public"
    export.export_site(out, "alice")
    html = (out / "index.html").read_text()
    # Assert on the REAL <body> open tag (a line that is exactly the tag),
    # not a bare substring — a `<body class="readonly">` mentioned in a
    # comment must NOT count. (That exact false-positive shipped once: the
    # real tag stayed plain and the browser hit the live /api/* paths.)
    assert re.search(
        r'(?m)^<body class="readonly" data-api-base="\./api" data-api-static="1">$',
        html,
    ), "real <body> tag was not gated"
    assert re.search(r"(?m)^<body>$", html) is None, "a plain <body> tag survived"
    # The static-fetch mechanism must ship: READONLY, API_BASE and API_STATIC
    # are read from the body, and dataPath() turns the four boot fetches into
    # ./api/*.json files.
    assert "classList.contains('readonly')" in html
    assert "document.body.dataset.apiBase || '/api'" in html
    assert "document.body.dataset.apiStatic === '1'" in html
    assert "function dataPath(name)" in html
    assert "fonts.googleapis.com" not in html  # Google Fonts dropped (T4)


def test_transform_html_for_a_live_read_only_view() -> None:
    # The same transform serves a live read-only page (API endpoints under
    # another base, not static files); the base is attribute-escaped.
    html = export.transform_html(
        "<head>\n<body>\n</body>", api_base="/u/a&b/api", static=False
    )
    assert '<body class="readonly" data-api-base="/u/a&amp;b/api">' in html
    assert "data-api-static" not in html


def test_transform_html_requires_a_body_tag() -> None:
    with pytest.raises(ValueError, match="no `<body>`"):
        export.transform_html("<body class='x'>", api_base="./api", static=True)


def test_export_html_transform_changed_the_source() -> None:
    # Guard against a no-op transform: the canonical source must have a plain
    # <body> open tag and the Google Fonts import — the export flips both.
    source = (export.STATIC_DIR / "index.html").read_text()
    assert re.search(r"(?m)^<body>$", source), "source lost its plain <body> tag"
    assert re.search(r'(?m)^<body class="readonly">$', source) is None
    assert "fonts.googleapis.com" in source
