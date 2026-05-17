"""Accounts and login sessions (`users` / `sessions` tables, see `schema.py`).

Passwords are stored as salted scrypt hashes (stdlib `hashlib.scrypt`);
the encoded hash carries its own parameters, so raising the cost later
keeps old hashes verifiable. A session is an unguessable random token
held only in the user's cookie — the DB stores its sha256, so a leaked DB
cannot be replayed as a login.

Each account also owns a secrets vault (see `vault`): its key is stored
wrapped under the password, so `register` and `login` — which hold the
password — are the only places it is unwrapped, and `set_password`
re-wraps it. An account created before vaults existed gets one at its
next login.
"""

import base64
import hashlib
import logging
import re
import secrets
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from wishmap import db, vault

logger = logging.getLogger(__name__)

# scrypt cost: N=2^15, r=8, p=1 takes ~60 ms and 32 MiB per hash. Module
# constants so tests can lower them; verification always uses the
# parameters stored in the hash, not these.
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
_SCRYPT_SALT_BYTES = 16
_SCRYPT_DKLEN = 32
# OpenSSL's default cap (32 MiB) is just below what N=2^15, r=8 needs.
_SCRYPT_MAXMEM = 64 * 1024 * 1024
# Each hash allocates tens of MiB; cap how many run at once so a burst of
# logins can't exhaust memory (FastAPI runs handlers on a ~40-thread pool).
_HASH_SLOTS = threading.BoundedSemaphore(4)

USERNAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{2,31}")
USERNAME_RULES = (
    "Usernames are 3-32 characters: letters, digits, '.', '_' or '-', "
    "starting with a letter or digit."
)
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 1024

# 32 random bytes -> 43 URL-safe characters.
SESSION_TOKEN_BYTES = 32
# `last_seen_at` is refreshed at most this often, so authenticated reads
# don't each cost a DB write.
_SESSION_TOUCH_INTERVAL = timedelta(minutes=5)


class UsernameTakenError(Exception):
    """Registration hit an existing username (case-insensitively)."""


@dataclass(frozen=True)
class User:
    id: int
    username: str


@dataclass(frozen=True)
class Login:
    """A verified sign-in: the account and its unwrapped vault key."""

    user: User
    vault_key: bytes = field(repr=False)


# How often `login` re-reads an account whose vault changed under it (a
# parallel login creating it, a password change) before giving up.
_VAULT_ATTEMPTS = 3


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    # Fixed precision so the stored timestamps also compare correctly as
    # strings (the expired-session sweep compares them in SQL).
    return dt.isoformat(timespec="seconds")


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int, dklen: int) -> bytes:
    with _HASH_SLOTS:
        return hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            maxmem=_SCRYPT_MAXMEM,
            dklen=dklen,
        )


def hash_password(password: str) -> str:
    """Return the encoded salted scrypt hash `scrypt$N$r$p$salt$hash`."""
    salt = secrets.token_bytes(_SCRYPT_SALT_BYTES)
    digest = _scrypt(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P, _SCRYPT_DKLEN)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    """True if `password` matches the hash from `hash_password`.

    A malformed hash never matches. The comparison is constant-time.
    """
    try:
        scheme, n, r, p, salt_b64, digest_b64 = encoded.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.urlsafe_b64decode(salt_b64)
        expected = base64.urlsafe_b64decode(digest_b64)
        actual = _scrypt(password, salt, int(n), int(r), int(p), len(expected))
    # Wrong field count, bad base64/int, scrypt parameters out of range.
    except (ValueError, OverflowError):
        return False
    return secrets.compare_digest(actual, expected)


# Verified against when a login names an unknown user, so that path costs
# the same scrypt as a wrong password (no username probing by timing).
_dummy_hash: str | None = None


def _burn_password_check(password: str) -> None:
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_password(secrets.token_urlsafe(16))
    verify_password(password, _dummy_hash)


def validate_new_credentials(username: str, password: str) -> None:
    """Raise ValueError (user-readable) unless both are acceptable for a new account."""
    if not USERNAME_RE.fullmatch(username):
        raise ValueError(USERNAME_RULES)
    validate_new_password(password)


def validate_new_password(password: str) -> None:
    """Raise ValueError (user-readable) unless `password` is acceptable."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(
            f"Passwords need at least {MIN_PASSWORD_LENGTH} characters."
        )
    if len(password) > MAX_PASSWORD_LENGTH:
        raise ValueError(
            f"Passwords can be at most {MAX_PASSWORD_LENGTH} characters."
        )


def register(db_path: Path, username: str, password: str) -> Login:
    """Create an account with a fresh vault; return it with the vault key.

    Raises ValueError for an invalid username/password and
    UsernameTakenError if the name exists (case-insensitively).
    """
    validate_new_credentials(username, password)
    pw_hash = hash_password(password)
    key = vault.new_key()
    try:
        with db.connect(db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "INSERT INTO users (username, pw_hash, created_at) VALUES (?, ?, ?)",
                (username, pw_hash, _iso(_now())),
            )
            user_id = cur.lastrowid
            assert user_id is not None
            # The wrapping binds the key to the user id, known only now;
            # same transaction, so no account ever exists without a vault.
            salt, wrapped = vault.wrap_key(key, password, user_id=user_id)
            conn.execute(
                "UPDATE users SET vault_salt = ?, vault_wrapped = ? WHERE id = ?",
                (salt, wrapped, user_id),
            )
    except sqlite3.IntegrityError as e:
        raise UsernameTakenError(username) from e
    return Login(user=User(id=user_id, username=username), vault_key=key)


def create_user(db_path: Path, username: str, password: str) -> User:
    """Create an account (with its vault) and return it; see `register`."""
    return register(db_path, username, password).user


def get_user(db_path: Path, user_id: int) -> User | None:
    with db.connect(db_path) as conn:
        row = conn.execute(
            "SELECT id, username FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    return None if row is None else User(id=row["id"], username=row["username"])


def get_by_username(db_path: Path, username: str) -> User | None:
    """Look a user up by name, case-insensitively."""
    with db.connect(db_path) as conn:
        row = conn.execute(
            "SELECT id, username FROM users WHERE username = ?", (username,)
        ).fetchone()
    return None if row is None else User(id=row["id"], username=row["username"])


def authenticate(db_path: Path, username: str, password: str) -> User | None:
    """Return the user if `password` is theirs, else None.

    An unknown username costs the same password check as a wrong
    password, so response time doesn't reveal which names exist.
    """
    with db.connect(db_path) as conn:
        row = conn.execute(
            "SELECT id, username, pw_hash FROM users WHERE username = ?",
            (username,),
        ).fetchone()
    if row is None:
        _burn_password_check(password)
        return None
    if not verify_password(password, row["pw_hash"]):
        return None
    return User(id=row["id"], username=row["username"])


_SELECT_LOGIN = "SELECT id, username, pw_hash, vault_salt, vault_wrapped FROM users"


def login(db_path: Path, username: str, password: str) -> Login | None:
    """Verify the password like `authenticate`, and unlock the account's vault.

    None for an unknown user or a wrong password. An account without a
    vault (created before vaults existed) gets one now. A vault that does
    not open with the account's correct password is unusable (e.g. the
    password was reset behind its back): it is replaced by a fresh one,
    dropping the provider tokens sealed under the old key — the user then
    logs into those providers again.
    """
    with db.connect(db_path) as conn:
        row = conn.execute(
            f"{_SELECT_LOGIN} WHERE username = ?", (username,)
        ).fetchone()
    if row is None:
        _burn_password_check(password)
        return None
    verified_hash: str | None = None
    for _ in range(_VAULT_ATTEMPTS):
        if row["pw_hash"] != verified_hash:
            if not verify_password(password, row["pw_hash"]):
                return None
            verified_hash = row["pw_hash"]
        key = _open_vault(db_path, row, password)
        if key is not None:
            return Login(user=User(id=row["id"], username=row["username"]), vault_key=key)
        # The account changed under us: look again.
        with db.connect(db_path) as conn:
            row = conn.execute(f"{_SELECT_LOGIN} WHERE id = ?", (row["id"],)).fetchone()
        if row is None:
            return None
    raise RuntimeError(f"the vault of user {row['id']} kept changing during login")


def _open_vault(db_path: Path, row: sqlite3.Row, password: str) -> bytes | None:
    """Unwrap the vault key of the account `row` (password already verified).

    Creates (or replaces an unusable) vault when needed. Returns None,
    writing nothing, if the account's password or vault changed since
    `row` was read — the caller re-reads and retries.
    """
    user_id: int = row["id"]
    wrapped: bytes | None = row["vault_wrapped"]
    salt: bytes | None = row["vault_salt"]
    if wrapped is not None and salt is not None:
        try:
            return vault.unwrap_key(wrapped, salt, password, user_id=user_id)
        except vault.VaultError:
            logger.error(
                "The vault of user %d does not open with their password; "
                "replacing it (their saved provider logins are lost)",
                user_id,
            )
    key = vault.new_key()
    new_salt, new_wrapped = vault.wrap_key(key, password, user_id=user_id)
    with db.connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "UPDATE users SET vault_salt = ?, vault_wrapped = ? "
            "WHERE id = ? AND pw_hash = ? AND vault_wrapped IS ?",
            (new_salt, new_wrapped, user_id, row["pw_hash"], wrapped),
        )
        if cur.rowcount == 0:
            return None
        # Nothing sealed under a previous key can be opened any more.
        _forget_sealed_secrets(conn, user_id)
    if wrapped is None:
        logger.info("Created the vault of user %d", user_id)
    return key


def vault_key_matches(db_path: Path, user_id: int, key: bytes) -> bool:
    """True if `key` is the user's current vault key (e.g. from their cookie)."""
    with db.connect(db_path) as conn:
        row = conn.execute(
            "SELECT vault_wrapped FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    if row is None or row["vault_wrapped"] is None:
        return False
    return vault.key_matches(row["vault_wrapped"], key, user_id=user_id)


def _forget_sealed_secrets(conn: sqlite3.Connection, user_id: int) -> None:
    conn.execute(
        "UPDATE integrations SET tokens_enc = NULL WHERE user_id = ?", (user_id,)
    )


def set_password(
    db_path: Path, user_id: int, new_password: str, *, vault_key: bytes | None = None
) -> None:
    """Replace the user's password (ValueError if it's unacceptable).

    With the account's `vault_key` (from `login`), the vault is re-wrapped
    under the new password, so the sealed provider tokens survive. Without
    it the vault can't follow the password: it is dropped, together with
    those tokens, and the next login creates a fresh one.
    """
    validate_new_password(new_password)
    pw_hash = hash_password(new_password)
    salt: bytes | None = None
    wrapped: bytes | None = None
    if vault_key is not None:
        salt, wrapped = vault.wrap_key(vault_key, new_password, user_id=user_id)
    with db.connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE users SET pw_hash = ?, vault_salt = ?, vault_wrapped = ? "
            "WHERE id = ?",
            (pw_hash, salt, wrapped, user_id),
        )
        if vault_key is None:
            _forget_sealed_secrets(conn, user_id)


# ── Sessions ─────────────────────────────────────────────────────────


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(db_path: Path, user_id: int, lifetime: timedelta) -> str:
    """Start a session for the user; return its token (stored only hashed).

    Also sweeps every expired session, which keeps the table bounded
    without a background job.
    """
    token = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
    now = _now()
    with db.connect(db_path) as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (_iso(now),))
        conn.execute(
            """
            INSERT INTO sessions
                (token_hash, user_id, created_at, last_seen_at, expires_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (_token_hash(token), user_id, _iso(now), _iso(now), _iso(now + lifetime)),
        )
    return token


def resolve_session(db_path: Path, token: str) -> User | None:
    """Return the user a session token belongs to, or None.

    None for an unknown, revoked or expired token (an expired one is
    deleted on the way).
    """
    # Real tokens are 43 chars; don't hash arbitrary junk.
    if not token or len(token) > 128:
        return None
    token_hash = _token_hash(token)
    now = _now()
    with db.connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT u.id, u.username, s.last_seen_at, s.expires_at
            FROM sessions s JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = ?
            """,
            (token_hash,),
        ).fetchone()
        if row is None:
            return None
        if datetime.fromisoformat(row["expires_at"]) <= now:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
            return None
        if now - datetime.fromisoformat(row["last_seen_at"]) >= _SESSION_TOUCH_INTERVAL:
            conn.execute(
                "UPDATE sessions SET last_seen_at = ? WHERE token_hash = ?",
                (_iso(now), token_hash),
            )
    return User(id=row["id"], username=row["username"])


def delete_session(db_path: Path, token: str) -> None:
    """Revoke one session (a no-op for an unknown token)."""
    with db.connect(db_path) as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))


def delete_sessions(db_path: Path, user_id: int, *, keep_token: str | None = None) -> int:
    """Revoke all of the user's sessions except `keep_token`'s; return how many."""
    keep_hash = "" if keep_token is None else _token_hash(keep_token)
    with db.connect(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM sessions WHERE user_id = ? AND token_hash != ?",
            (user_id, keep_hash),
        )
        return cur.rowcount
