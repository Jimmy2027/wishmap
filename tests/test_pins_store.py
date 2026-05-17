"""Unit tests for the pins_store DB module (tmp_path DB, no HTTP layer)."""

import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from wishmap import pins_store
from wishmap.models import PinConfig, Status

USER = 1
OTHER = 2


def _pin(pin_id: str = "pin-1", **overrides: object) -> PinConfig:
    base: dict[str, object] = {
        "id": pin_id,
        "name": "Summit",
        "lat": 46.5,
        "lon": 9.75,
        "sport": ["hiking", "skitouring"],
        "status": "idea",
        "tags": ["view"],
        "notes": "bring crampons",
    }
    base.update(overrides)
    return PinConfig(**base)  # type: ignore[arg-type]


def _created_at(db_path: Path, user_id: int, pin_id: str) -> str:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT created_at FROM pins WHERE user_id = ? AND id = ?",
            (user_id, pin_id),
        ).fetchone()[0]
    finally:
        conn.close()


def test_get_all_empty(db_path: Path) -> None:
    assert pins_store.get_all(db_path, USER) == []


def test_insert_round_trips_pin_config(db_path: Path) -> None:
    assert pins_store.insert(db_path, USER, _pin()) is True
    pins = pins_store.get_all(db_path, USER)
    assert pins == [_pin()]
    assert pins[0].sport == ["hiking", "skitouring"]  # JSON array round-trip
    assert pins[0].status == Status.IDEA
    assert pins_store.get(db_path, USER, "pin-1") == _pin()


def test_insert_same_id_replaces_and_preserves_created_at(db_path: Path) -> None:
    pins_store.insert(db_path, USER, _pin(), created_at="2020-01-01T00:00:00+00:00")
    created = pins_store.insert(
        db_path, USER, _pin(name="Renamed"), created_at="2099-01-01T00:00:00+00:00"
    )
    assert created is False
    assert pins_store.get_all(db_path, USER) == [_pin(name="Renamed")]
    assert _created_at(db_path, USER, "pin-1") == "2020-01-01T00:00:00+00:00"


def test_get_all_orders_by_created_at(db_path: Path) -> None:
    pins_store.insert(db_path, USER, _pin("b"), created_at="2022-01-01T00:00:00+00:00")
    pins_store.insert(db_path, USER, _pin("a"), created_at="2021-01-01T00:00:00+00:00")
    assert [p.id for p in pins_store.get_all(db_path, USER)] == ["a", "b"]


def test_delete(db_path: Path) -> None:
    pins_store.insert(db_path, USER, _pin())
    assert pins_store.delete(db_path, USER, "pin-1") is True
    assert pins_store.delete(db_path, USER, "pin-1") is False
    assert pins_store.get_all(db_path, USER) == []


def test_pins_are_scoped_per_user(db_path: Path) -> None:
    pins_store.insert(db_path, OTHER, _pin())
    assert pins_store.get_all(db_path, USER) == []
    assert pins_store.get(db_path, USER, "pin-1") is None
    assert pins_store.update(db_path, USER, "pin-1", {"name": "X"}) is None
    assert pins_store.delete(db_path, USER, "pin-1") is False
    # Same id for another user is an independent row.
    assert pins_store.insert(db_path, USER, _pin(name="Mine")) is True
    assert pins_store.get(db_path, OTHER, "pin-1") == _pin()


def test_update_changes_only_patched_fields(db_path: Path) -> None:
    pins_store.insert(db_path, USER, _pin())
    out = pins_store.update(
        db_path, USER, "pin-1", {"lat": 47.0, "status": "done", "sport": "biking"}
    )
    assert out == _pin(lat=47.0, status="done", sport=["biking"])
    assert pins_store.get(db_path, USER, "pin-1") == out


def test_update_missing_returns_none(db_path: Path) -> None:
    assert pins_store.update(db_path, USER, "nope", {"name": "X"}) is None


def test_update_invalid_value_raises_and_writes_nothing(db_path: Path) -> None:
    pins_store.insert(db_path, USER, _pin())
    with pytest.raises(ValidationError):
        pins_store.update(db_path, USER, "pin-1", {"lat": "north"})
    assert pins_store.get(db_path, USER, "pin-1") == _pin()


def test_update_rejects_id_change(db_path: Path) -> None:
    pins_store.insert(db_path, USER, _pin())
    with pytest.raises(ValueError, match="id"):
        pins_store.update(db_path, USER, "pin-1", {"id": "pin-2"})
