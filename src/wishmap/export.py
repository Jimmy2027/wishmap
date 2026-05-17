"""Static read-only snapshot export (`wishmap export --user NAME <dir>`).

Writes a self-contained static site — `index.html` + `api/{config,pins,
routes,ratings}.json` — that serves a read-only view of one account's map.
It can be published to a free static host with **no server, no write
surface, and no secrets** on the public web.

This is the live server's read path minus the HTTP layer: it replicates the
lifespan setup (settings load + data dir mkdir), reuses `userdata.build`
and `ratings.get_all` for the account, and bakes the read-only gate into
the copied `index.html` at export time (so there is no flash of edit
controls and the gate holds with JS off).

Side effect: opening the DB runs the schema migration/creation, so an
export on a box with no `wishmap.db` *materializes an empty DB* (and then
fails: there is no account to export). It never edits existing rows, but
the build fills the account's `track_cache` (a pure cache).
"""

import json
import re
from html import escape as html_escape
from pathlib import Path

from wishmap import ratings, userdata, users
from wishmap.config import load_settings, resolve_config_path, resolve_data_root
from wishmap.models import ConfigResponse, RouteRating
from wishmap.state import user_dir

STATIC_DIR = Path(__file__).parent / "static"

# Matches ONLY the real <body> open tag — a line that is exactly `<body>`.
# Anchored to line start/end so a `<body>` that appears as prose inside a CSS
# or JS comment is never matched (that footgun shipped a broken export once).
# `\r?` tolerates a CRLF checkout so the gate still bakes on Windows.
_BODY_TAG_RE = re.compile(r"^<body>\r?$", re.MULTILINE)

# Drops the Google Fonts @import from the exported CSS. Google Fonts logs
# every public viewer's IP, which a privacy-conscious local-first showcase
# should not leak; the --font-body / --font-display vars already carry
# system-font fallbacks (system-ui / Georgia), so the page degrades cleanly.
_GOOGLE_FONTS_IMPORT_RE = re.compile(
    r"^[^\n]*@import url\([^)]*fonts\.googleapis\.com[^)]*\);[^\n]*\n",
    re.MULTILINE,
)


def transform_html(html: str, *, api_base: str, static: bool) -> str:
    """Bake a read-only view into a copy of index.html.

    Shared by the static export (`api_base="./api"`, `static=True`) and
    the server's public link page `/u/<token>` (`api_base="/u/<token>/api"`,
    live endpoints).

    Two string-level edits keep the single source file canonical:
      1. `<body>` -> `<body class="readonly" data-api-base=… [data-api-static="1"]>`.
         The page derives its READONLY flag from the class (and
         `body.readonly` CSS hides every edit control and the login screen),
         reads its data from `api_base` instead of `/api`, and — when
         `static` — fetches `<name>.json` files rather than API endpoints.
      2. Drop the Google Fonts @import (per-visitor IP leak).
    """
    attrs = f'class="readonly" data-api-base="{html_escape(api_base, quote=True)}"'
    if static:
        attrs += ' data-api-static="1"'
    html, n = _BODY_TAG_RE.subn(f"<body {attrs}>", html, count=1)
    if n != 1:
        raise ValueError("index.html has no `<body>` open tag on its own line")
    html = _GOOGLE_FONTS_IMPORT_RE.sub("", html)
    return html


class ExportError(Exception):
    """The export cannot proceed (user-readable message)."""


def export_site(out_dir: Path, username: str) -> list[Path]:
    """Build the static read-only snapshot of `username`'s map under `out_dir`.

    Returns the files written; overwrites any existing files at the same
    paths. Mirrors the lifespan startup (app.py): resolve config, mkdir
    the data dir before opening the DB, build the account's GeoJSON, read
    its ratings, then write the static artifact. Raises ExportError (and
    writes nothing) for an unknown account.
    """
    config_path = resolve_config_path()
    settings = load_settings(config_path)
    data_root = resolve_data_root(config_path, settings)
    db_path = data_root / "wishmap.db"
    # Replicate lifespan's mkdir (app.py): sqlite3 raises "unable to open"
    # if the data dir is missing on a fresh checkout.
    data_root.mkdir(parents=True, exist_ok=True)

    user = users.get_by_username(db_path, username)
    if user is None:
        raise ExportError(f"No account named {username!r}")

    try:
        pins_fc, routes_fc = userdata.build(
            user_dir(data_root, user.id), db_path, user.id
        )
    except Exception as e:  # GPX parse (a file changed mid-build), DB errors
        raise ExportError(f"Building the map failed: {e}") from e
    # model_dump(mode="json") gives JSON-native values (enums -> .value,
    # datetimes -> ISO), matching FastAPI's serializer and keeping json.dumps
    # below safe even if a model later grows a non-native field.
    # Match GET /api/ratings exactly: a dict keyed by route_id, not a list.
    ratings_by_id = {
        route_id: RouteRating(**row).model_dump(mode="json")
        for route_id, row in ratings.get_all(db_path, user.id).items()
    }
    # The export's only readonly carrier in JSON; the live /api/config stays
    # readonly=False. Secrets never reach here — ConfigResponse is the title
    # plus flags.
    config_payload = ConfigResponse(
        title=settings.title, readonly=True
    ).model_dump(mode="json")

    out_dir = Path(out_dir)
    api_dir = out_dir / "api"
    api_dir.mkdir(parents=True, exist_ok=True)

    index_path = out_dir / "index.html"
    index_path.write_text(
        transform_html(
            (STATIC_DIR / "index.html").read_text(), api_base="./api", static=True
        )
    )


    written = [index_path]
    for name, payload in (
        ("config", config_payload),
        ("pins", pins_fc.model_dump(mode="json")),
        ("routes", routes_fc.model_dump(mode="json")),
        ("ratings", ratings_by_id),
    ):
        path = api_dir / f"{name}.json"
        # Compact separators: a static snapshot is download-then-parse, so the
        # default ", " / ": " whitespace is pure transfer weight.
        path.write_text(json.dumps(payload, separators=(",", ":")))
        written.append(path)
    return written
