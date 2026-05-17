"""Unit tests for the shared_maps DB module (tmp_path DB, no HTTP layer)."""

import sqlite3
from pathlib import Path

import pytest

from wishmap import db, shared_maps


def _add_user(db_path: Path, username: str) -> int:
    with db.connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO users (username, pw_hash, created_at) VALUES (?, 'h', 'now')",
            (username,),
        )
    assert cur.lastrowid is not None
    return cur.lastrowid


@pytest.fixture
def alice(db_path: Path) -> int:
    return _add_user(db_path, "alice")


@pytest.fixture
def bob(db_path: Path) -> int:
    return _add_user(db_path, "bob")


def test_not_shared_by_default(db_path: Path, alice: int, bob: int) -> None:
    assert shared_maps.is_shared(db_path, alice) is False
    assert shared_maps.list_shared(db_path, exclude_user_id=bob) == []


def test_enable_is_idempotent(db_path: Path, alice: int, bob: int) -> None:
    shared_maps.set_shared(db_path, alice, True)
    shared_maps.set_shared(db_path, alice, True)
    assert shared_maps.is_shared(db_path, alice) is True
    assert shared_maps.list_shared(db_path, exclude_user_id=bob) == [(alice, "alice")]


def test_disable(db_path: Path, alice: int, bob: int) -> None:
    shared_maps.set_shared(db_path, alice, True)
    shared_maps.set_shared(db_path, alice, False)
    assert shared_maps.is_shared(db_path, alice) is False
    assert shared_maps.list_shared(db_path, exclude_user_id=bob) == []
    shared_maps.set_shared(db_path, alice, False)  # already off: no error
    assert shared_maps.is_shared(db_path, alice) is False


def test_list_excludes_viewer_and_non_sharers_and_is_sorted(
    db_path: Path, alice: int, bob: int
) -> None:
    zoe = _add_user(db_path, "Zoe")
    carl = _add_user(db_path, "carl")
    _add_user(db_path, "dora")  # never shares
    for uid in (zoe, carl, alice, bob):
        shared_maps.set_shared(db_path, uid, True)
    # Case-insensitive order; the viewer (bob) isn't offered to himself.
    assert shared_maps.list_shared(db_path, exclude_user_id=bob) == [
        (alice, "alice"),
        (carl, "carl"),
        (zoe, "Zoe"),
    ]


def test_deleting_user_cascades(db_path: Path, alice: int, bob: int) -> None:
    shared_maps.set_shared(db_path, alice, True)
    with db.connect(db_path) as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (alice,))
        assert conn.execute("SELECT COUNT(*) FROM shared_maps").fetchone()[0] == 0
    assert shared_maps.list_shared(db_path, exclude_user_id=bob) == []


def test_enable_requires_existing_user(db_path: Path) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        shared_maps.set_shared(db_path, 999, True)
