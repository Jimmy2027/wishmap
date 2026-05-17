"""Unit tests for accounts, password hashing and sessions (`users`)."""

import hashlib
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from wishmap import db, users


def _create(db_path: Path, name: str = "alice", password: str = "s3cret-pw") -> users.User:
    return users.create_user(db_path, name, password)


# ── Password hashing ─────────────────────────────────────────────────


def test_hash_round_trip_and_wrong_password() -> None:
    encoded = users.hash_password("hunter2hunter2")
    assert users.verify_password("hunter2hunter2", encoded)
    assert not users.verify_password("hunter2hunter3", encoded)


def test_hash_is_salted_and_self_describing() -> None:
    a = users.hash_password("same password")
    b = users.hash_password("same password")
    assert a != b
    scheme, n, r, p, _salt, _digest = a.split("$")
    assert (scheme, int(n), int(r), int(p)) == (
        "scrypt",
        users.SCRYPT_N,
        users.SCRYPT_R,
        users.SCRYPT_P,
    )
    assert "same password" not in a


def test_hash_verifies_with_its_own_parameters(monkeypatch: pytest.MonkeyPatch) -> None:
    # Raising the cost later must not lock out existing accounts.
    old = users.hash_password("pw-before-upgrade")
    monkeypatch.setattr(users, "SCRYPT_N", users.SCRYPT_N * 2)
    assert users.verify_password("pw-before-upgrade", old)


def test_production_cost_parameters_work(monkeypatch: pytest.MonkeyPatch) -> None:
    # The suite runs with a cheap N (conftest); the real one must be
    # accepted by OpenSSL within the memory cap.
    monkeypatch.undo()
    assert users.SCRYPT_N == 2**15
    encoded = users.hash_password("production-cost")
    assert encoded.startswith(f"scrypt${2**15}$8$1$")
    assert users.verify_password("production-cost", encoded)


@pytest.mark.parametrize(
    "encoded",
    [
        "",
        "plain-text-password",
        "bcrypt$1$2$3$c2FsdA==$ZGlnZXN0",
        "scrypt$notanint$8$1$c2FsdA==$ZGlnZXN0",
        "scrypt$16$8$1$!!!$ZGlnZXN0",
        "scrypt$15$8$1$c2FsdA==$ZGlnZXN0",  # N not a power of two
        f"scrypt${2**40}$8$1$c2FsdA==$ZGlnZXN0",  # exceeds the memory cap
    ],
)
def test_malformed_hash_never_matches(encoded: str) -> None:
    assert not users.verify_password("anything", encoded)


# ── Accounts ─────────────────────────────────────────────────────────


def test_create_and_authenticate(db_path: Path) -> None:
    alice = _create(db_path)
    assert alice.id > 0 and alice.username == "alice"
    assert users.authenticate(db_path, "alice", "s3cret-pw") == alice
    assert users.authenticate(db_path, "alice", "wrong-pw") is None
    assert users.authenticate(db_path, "nobody", "s3cret-pw") is None


def test_password_is_stored_hashed(db_path: Path) -> None:
    _create(db_path, password="plaintext-canary")
    with db.connect(db_path) as conn:
        (pw_hash,) = conn.execute("SELECT pw_hash FROM users").fetchone()
    assert pw_hash.startswith("scrypt$")
    assert "plaintext-canary" not in pw_hash


def test_usernames_are_case_insensitive(db_path: Path) -> None:
    alice = _create(db_path, "Alice")
    with pytest.raises(users.UsernameTakenError):
        _create(db_path, "alice")
    # Login and lookup ignore case; the stored spelling is kept.
    assert users.authenticate(db_path, "ALICE", "s3cret-pw") == alice
    assert users.get_by_username(db_path, "aLiCe") == alice
    assert users.get_user(db_path, alice.id) == alice


@pytest.mark.parametrize(
    "name", ["", "ab", "a" * 33, "has space", "-leading", "évil", "a/b", "../x"]
)
def test_invalid_usernames_rejected(db_path: Path, name: str) -> None:
    with pytest.raises(ValueError, match="Usernames are"):
        _create(db_path, name)


@pytest.mark.parametrize("name", ["abc", "Alice_2", "a.b-c", "x" * 32])
def test_valid_usernames_accepted(db_path: Path, name: str) -> None:
    assert _create(db_path, name).username == name


def test_short_and_huge_passwords_rejected(db_path: Path) -> None:
    with pytest.raises(ValueError, match="at least"):
        _create(db_path, password="short")
    with pytest.raises(ValueError, match="at most"):
        _create(db_path, password="x" * (users.MAX_PASSWORD_LENGTH + 1))


def test_set_password(db_path: Path) -> None:
    alice = _create(db_path)
    users.set_password(db_path, alice.id, "brand-new-pw")
    assert users.authenticate(db_path, "alice", "s3cret-pw") is None
    assert users.authenticate(db_path, "alice", "brand-new-pw") == alice
    with pytest.raises(ValueError):
        users.set_password(db_path, alice.id, "short")


def test_user_ids_never_collide_with_unclaimed(db_path: Path) -> None:
    # id 0 owns pre-account rows; the schema forbids it for real accounts.
    with pytest.raises(sqlite3.IntegrityError):
        with db.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO users (id, username, pw_hash, created_at) "
                "VALUES (0, 'zero', 'x', 'now')"
            )


# ── Sessions ─────────────────────────────────────────────────────────


def test_session_round_trip_stores_only_a_hash(db_path: Path) -> None:
    alice = _create(db_path)
    token = users.create_session(db_path, alice.id, timedelta(days=30))
    assert len(token) >= 43
    assert users.resolve_session(db_path, token) == alice
    with db.connect(db_path) as conn:
        (stored,) = conn.execute("SELECT token_hash FROM sessions").fetchone()
    assert stored == hashlib.sha256(token.encode()).hexdigest()
    assert token not in stored


@pytest.mark.parametrize("token", ["", "nope", "x" * 500])
def test_unknown_token_resolves_to_none(db_path: Path, token: str) -> None:
    _create(db_path)
    assert users.resolve_session(db_path, token) is None


def test_expired_session_is_rejected_and_deleted(db_path: Path) -> None:
    alice = _create(db_path)
    token = users.create_session(db_path, alice.id, timedelta(seconds=-1))
    assert users.resolve_session(db_path, token) is None
    with db.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_creating_a_session_sweeps_expired_ones(db_path: Path) -> None:
    alice = _create(db_path)
    users.create_session(db_path, alice.id, timedelta(seconds=-1))
    users.create_session(db_path, alice.id, timedelta(seconds=-1))
    users.create_session(db_path, alice.id, timedelta(days=1))
    with db.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1


def test_resolve_touches_last_seen_at_only_when_stale(db_path: Path) -> None:
    alice = _create(db_path)
    token = users.create_session(db_path, alice.id, timedelta(days=1))
    with db.connect(db_path) as conn:
        conn.execute("UPDATE sessions SET last_seen_at = '2000-01-01T00:00:00+00:00'")
    users.resolve_session(db_path, token)
    with db.connect(db_path) as conn:
        (seen,) = conn.execute("SELECT last_seen_at FROM sessions").fetchone()
    assert seen > "2000-01-01T00:00:00+00:00"
    users.resolve_session(db_path, token)
    with db.connect(db_path) as conn:
        assert conn.execute("SELECT last_seen_at FROM sessions").fetchone()[0] == seen


def test_delete_session_and_delete_sessions(db_path: Path) -> None:
    alice = _create(db_path)
    bob = _create(db_path, "bob")
    a1 = users.create_session(db_path, alice.id, timedelta(days=1))
    a2 = users.create_session(db_path, alice.id, timedelta(days=1))
    a3 = users.create_session(db_path, alice.id, timedelta(days=1))
    b1 = users.create_session(db_path, bob.id, timedelta(days=1))

    users.delete_session(db_path, a1)
    assert users.resolve_session(db_path, a1) is None
    users.delete_session(db_path, "never-existed")  # no-op

    assert users.delete_sessions(db_path, alice.id, keep_token=a2) == 1
    assert users.resolve_session(db_path, a2) == alice
    assert users.resolve_session(db_path, a3) is None
    assert users.resolve_session(db_path, b1) == bob  # other users untouched

    assert users.delete_sessions(db_path, alice.id) == 1
    assert users.resolve_session(db_path, a2) is None


def test_deleting_a_user_cascades_to_sessions(db_path: Path) -> None:
    alice = _create(db_path)
    token = users.create_session(db_path, alice.id, timedelta(days=1))
    with db.connect(db_path) as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (alice.id,))
    assert users.resolve_session(db_path, token) is None
