"""Tests for the server settings (`config.load_settings`) and their use at startup."""

import logging
import tomllib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.helpers import ctx
from wishmap.app import app
from wishmap.config import ConfigError, load_settings
from wishmap.models import ServerSettings, WishmapConfig


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "wishmap.toml"
    path.write_text(text)
    return path


def test_defaults(tmp_path: Path) -> None:
    assert load_settings(_write(tmp_path, "")) == ServerSettings(
        title="wishmap",
        allow_registration=True,
        secure_cookies=False,
        session_days=30,
        data_dir="data",
    )


def test_values(tmp_path: Path) -> None:
    settings = load_settings(
        _write(
            tmp_path,
            'title = "Atlas"\n'
            "allow_registration = false\n"
            "secure_cookies = true\n"
            "session_days = 7\n"
            'data_dir = "/srv/wishmap"\n',
        )
    )
    assert settings.title == "Atlas"
    assert settings.allow_registration is False
    assert settings.secure_cookies is True
    assert settings.session_days == 7
    assert settings.data_dir == "/srv/wishmap"


def test_legacy_and_unknown_keys_warn_but_load(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = _write(
        tmp_path,
        'title = "Old"\n'
        'includes = ["x.toml"]\n'
        "typo_key = 1\n"
        "[garmin]\n"
        'username = "u"\n'
        "[[pins]]\n"
        'id = "p"\n',
    )
    with caplog.at_level(logging.WARNING, logger="wishmap.config"):
        settings = load_settings(path)
    assert settings.title == "Old"
    messages = [r.getMessage() for r in caplog.records]
    # All legacy keys share one note (Garmin settings are per account now,
    # like pins and routes), which says how to import them; an unknown key
    # gets its own warning.
    legacy = [m for m in messages if "single-user data" in m]
    assert len(legacy) == 1
    assert "(garmin, includes, pins)" in legacy[0]
    assert f"wishmap import-toml --user NAME {path}" in legacy[0]
    assert [m for m in messages if m not in legacy] == [
        f"{path}: ignoring unknown key 'typo_key'"
    ]


REPO_TOML = Path(__file__).parent.parent / "wishmap.toml"


def test_repo_config_loads_with_one_note(caplog: pytest.LogCaptureFixture) -> None:
    # The checked-in wishmap.toml documents every server setting and keeps
    # its single-user data for `import-toml`: no typo'd setting, and the
    # kept data costs exactly one log line.
    with caplog.at_level(logging.WARNING, logger="wishmap.config"):
        settings = load_settings(REPO_TOML)
    with open(REPO_TOML, "rb") as f:
        data = tomllib.load(f)
    fields = set(ServerSettings.model_fields)
    assert fields <= data.keys()
    assert settings == ServerSettings(**{k: data[k] for k in fields})
    assert len(caplog.records) == 1
    assert "single-user data" in caplog.records[0].getMessage()
    # ... and that data is still a valid import (its includes and GPX files
    # are install data, checked by the import itself).
    legacy = WishmapConfig(**data)
    assert legacy.pins and legacy.routes and legacy.garmin is not None


def test_readme_documents_every_setting() -> None:
    readme = (REPO_TOML.parent / "README.md").read_text()
    for field in ServerSettings.model_fields:
        assert f"| `{field}` |" in readme, f"README settings table lacks {field}"
    # wishmap.toml points readers at this section by name.
    section = "Moving a single-user install into an account"
    assert section in REPO_TOML.read_text()
    assert f"\n## {section}\n" in readme


def test_legacy_key_warnings_can_be_silenced(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = _write(tmp_path, 'includes = ["x.toml"]\ntypo_key = 1\n')
    with caplog.at_level(logging.WARNING, logger="wishmap.config"):
        load_settings(path, warn_legacy=False)
    messages = "\n".join(r.getMessage() for r in caplog.records)
    assert "includes" not in messages
    assert "unknown key 'typo_key'" in messages


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("title = [", "Invalid TOML"),
        ("session_days = 0\n", "Invalid settings"),
        ('allow_registration = "maybe"\n', "Invalid settings"),
    ],
)
def test_invalid_settings_raise(tmp_path: Path, text: str, match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        load_settings(_write(tmp_path, text))


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_settings(tmp_path / "nope.toml")


@pytest.mark.parametrize("wishmap_toml", ['data_dir = "state"\n'])
def test_data_dir_is_relative_to_the_config(install: Path, client: TestClient) -> None:
    assert ctx().data_root == install / "state"
    assert ctx().db_path == install / "state" / "wishmap.db"
    assert ctx().db_path.is_file()
    assert ctx().user_dir(3) == install / "state" / "users" / "3"


# A server TOML from the single-user days: legacy pins/routes/includes and
# [garmin]/[strava] sections are ignored, so even broken ones must not keep
# the server from starting.
LEGACY_TOML = (
    'title = "Old"\n'
    'includes = ["broken.toml"]\n'
    "\n"
    "[garmin]\n"
    'username = "u@example.com"\n'
    "\n"
    "[[pins]]\n"
    'id = "p1"\n'
    'status = "someday"\n'
    "\n"
    "[[routes]]\n"
    'id = "r1"\n'
    'name = "x"\n'
    'sport = "hiking"\n'
    'status = "bogus"\n'
    'gpx = "missing.gpx"\n'
)


@pytest.mark.parametrize("wishmap_toml", [LEGACY_TOML])
def test_broken_legacy_entries_do_not_block_startup(install: Path) -> None:
    (install / "broken.toml").write_text("this is not toml")
    with TestClient(app) as c:
        assert c.get("/api/config").json()["title"] == "Old"


@pytest.mark.parametrize(
    "wishmap_toml", ['sync_user = "alice"\n[garmin]\nactivity_limit = 5\n']
)
def test_old_sync_keys_no_longer_matter(install: Path) -> None:
    # The interim host-account sync is gone: its `sync_user` and an
    # incomplete [garmin] section are just ignored keys now.
    with TestClient(app) as c:
        assert c.get("/api/config").status_code == 200
