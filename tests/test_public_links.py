"""Unit tests for the public_links DB module (tmp_path DB, no HTTP layer)."""

import sqlite3
from pathlib import Path

import pytest

from wishmap import db, public_links


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


def test_no_link_by_default(db_path: Path, alice: int) -> None:
    assert public_links.get_token(db_path, alice) is None
    assert public_links.resolve(db_path, "nope") is None
    assert public_links.revoke(db_path, alice) is False


def test_rotate_creates_link(db_path: Path, alice: int) -> None:
    token = public_links.rotate(db_path, alice)
    assert len(token) >= 43  # 32 random bytes, URL-safe base64
    assert public_links.get_token(db_path, alice) == token
    assert public_links.resolve(db_path, token) == alice


def test_rotate_replaces_token(db_path: Path, alice: int) -> None:
    old = public_links.rotate(db_path, alice)
    new = public_links.rotate(db_path, alice)
    assert new != old
    assert public_links.resolve(db_path, old) is None
    assert public_links.resolve(db_path, new) == alice
    assert public_links.get_token(db_path, alice) == new


def test_revoke(db_path: Path, alice: int) -> None:
    token = public_links.rotate(db_path, alice)
    assert public_links.revoke(db_path, alice) is True
    assert public_links.resolve(db_path, token) is None
    assert public_links.get_token(db_path, alice) is None
    assert public_links.revoke(db_path, alice) is False


def test_links_are_per_user(db_path: Path, alice: int, bob: int) -> None:
    a = public_links.rotate(db_path, alice)
    b = public_links.rotate(db_path, bob)
    assert public_links.resolve(db_path, a) == alice
    assert public_links.resolve(db_path, b) == bob
    public_links.revoke(db_path, alice)
    assert public_links.resolve(db_path, b) == bob


def test_rotate_requires_existing_user(db_path: Path) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        public_links.rotate(db_path, 999)
