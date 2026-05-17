"""Unit tests for the integrations_store DB module (tmp_path DB, no HTTP layer)."""

import sqlite3
from pathlib import Path

import pytest

from wishmap import integrations_store

USER = 1
OTHER = 2

SETTINGS = {"activity_limit": 50, "sport_filter": ["cycling"]}


def _configure(
    db_path: Path, user_id: int = USER, username: str = "me@example.com"
) -> integrations_store.Integration:
    return integrations_store.upsert_settings(
        db_path, user_id, "garmin", username=username, settings=SETTINGS
    )


def test_get_missing(db_path: Path) -> None:
    assert integrations_store.get(db_path, USER, "garmin") is None
    assert integrations_store.get_all(db_path, USER) == []


def test_upsert_settings_round_trips(db_path: Path) -> None:
    out = _configure(db_path)
    assert out.provider == "garmin"
    assert out.username == "me@example.com"
    assert out.settings == SETTINGS  # JSON object round-trip
    assert out.tokens_enc is None
    assert integrations_store.get(db_path, USER, "garmin") == out


def test_upsert_settings_replaces_settings_and_keeps_tokens(db_path: Path) -> None:
    _configure(db_path)
    assert integrations_store.set_tokens(db_path, USER, "garmin", b"sealed") is True
    out = integrations_store.upsert_settings(
        db_path, USER, "garmin", username="me@example.com", settings={}
    )
    assert out.settings == {}
    assert out.tokens_enc == b"sealed"


def test_changing_username_drops_tokens(db_path: Path) -> None:
    # Tokens belong to one provider account; they must not outlive a switch.
    _configure(db_path)
    integrations_store.set_tokens(db_path, USER, "garmin", b"sealed")
    out = _configure(db_path, username="other@example.com")
    assert out.username == "other@example.com"
    assert out.tokens_enc is None


def test_set_tokens_none_forgets_login(db_path: Path) -> None:
    _configure(db_path)
    integrations_store.set_tokens(db_path, USER, "garmin", b"sealed")
    assert integrations_store.set_tokens(db_path, USER, "garmin", None) is True
    out = integrations_store.get(db_path, USER, "garmin")
    assert out is not None
    assert out.tokens_enc is None
    assert out.settings == SETTINGS


def test_set_tokens_without_integration_writes_nothing(db_path: Path) -> None:
    assert integrations_store.set_tokens(db_path, USER, "garmin", b"sealed") is False
    assert integrations_store.get(db_path, USER, "garmin") is None


def test_tokens_stored_as_blob(db_path: Path) -> None:
    _configure(db_path)
    integrations_store.set_tokens(db_path, USER, "garmin", b"\x00\xffsealed")
    conn = sqlite3.connect(db_path)
    try:
        value = conn.execute("SELECT tokens_enc FROM integrations").fetchone()[0]
    finally:
        conn.close()
    assert value == b"\x00\xffsealed"


def test_delete(db_path: Path) -> None:
    _configure(db_path)
    assert integrations_store.delete(db_path, USER, "garmin") is True
    assert integrations_store.delete(db_path, USER, "garmin") is False
    assert integrations_store.get(db_path, USER, "garmin") is None


def test_get_all_lists_providers(db_path: Path) -> None:
    _configure(db_path)
    integrations_store.upsert_settings(
        db_path, USER, "strava", username=None, settings={}
    )
    assert [i.provider for i in integrations_store.get_all(db_path, USER)] == [
        "garmin",
        "strava",
    ]


def test_integrations_are_scoped_per_user(db_path: Path) -> None:
    _configure(db_path, OTHER)
    integrations_store.set_tokens(db_path, OTHER, "garmin", b"theirs")
    assert integrations_store.get(db_path, USER, "garmin") is None
    assert integrations_store.get_all(db_path, USER) == []
    assert integrations_store.set_tokens(db_path, USER, "garmin", b"x") is False
    assert integrations_store.delete(db_path, USER, "garmin") is False
    other = integrations_store.get(db_path, OTHER, "garmin")
    assert other is not None and other.tokens_enc == b"theirs"


def test_unknown_provider_rejected(db_path: Path) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        integrations_store.upsert_settings(
            db_path, USER, "komoot", username=None, settings={}  # type: ignore[arg-type]
        )
