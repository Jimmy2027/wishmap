"""Tests for the secrets vault (`vault`) and the accounts' vault keys (`users`)."""

from pathlib import Path

import pytest

from wishmap import db, integrations_store, users, vault

PASSWORD = "correct horse battery"


# ── Key wrapping ─────────────────────────────────────────────────────


def test_wrap_round_trip() -> None:
    key = vault.new_key()
    salt, wrapped = vault.wrap_key(key, PASSWORD, user_id=1)
    assert len(salt) == vault.SALT_BYTES
    assert key not in wrapped
    assert vault.unwrap_key(wrapped, salt, PASSWORD, user_id=1) == key


def test_wrapping_is_salted() -> None:
    key = vault.new_key()
    first = vault.wrap_key(key, PASSWORD, user_id=1)
    second = vault.wrap_key(key, PASSWORD, user_id=1)
    assert first[0] != second[0] and first[1] != second[1]


def test_wrong_password_does_not_unwrap() -> None:
    salt, wrapped = vault.wrap_key(vault.new_key(), PASSWORD, user_id=1)
    with pytest.raises(vault.VaultError):
        vault.unwrap_key(wrapped, salt, "not the password", user_id=1)


def test_wrapped_key_is_bound_to_its_user() -> None:
    """Copying one account's vault columns onto another account is useless."""
    salt, wrapped = vault.wrap_key(vault.new_key(), PASSWORD, user_id=1)
    with pytest.raises(vault.VaultError):
        vault.unwrap_key(wrapped, salt, PASSWORD, user_id=2)


@pytest.mark.parametrize("index", [0, 1, 5, 25, -1])
def test_tampered_wrapped_key_does_not_unwrap(index: int) -> None:
    salt, wrapped = vault.wrap_key(vault.new_key(), PASSWORD, user_id=1)
    tampered = bytearray(wrapped)
    tampered[index] ^= 0x01
    with pytest.raises(vault.VaultError):
        vault.unwrap_key(bytes(tampered), salt, PASSWORD, user_id=1)


def test_unwrap_uses_the_stored_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    key = vault.new_key()
    monkeypatch.setattr(vault, "SCRYPT_N", 2**5)
    salt, wrapped = vault.wrap_key(key, PASSWORD, user_id=1)
    monkeypatch.setattr(vault, "SCRYPT_N", 2**6)
    assert vault.unwrap_key(wrapped, salt, PASSWORD, user_id=1) == key


def test_production_cost_parameters_work(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vault, "SCRYPT_N", 2**15)
    key = vault.new_key()
    salt, wrapped = vault.wrap_key(key, PASSWORD, user_id=1)
    assert wrapped[1] == 15
    assert vault.unwrap_key(wrapped, salt, PASSWORD, user_id=1) == key


@pytest.mark.parametrize("wrapped", [b"", b"\x01", b"\x02" + bytes(60)])
def test_malformed_wrapped_key(wrapped: bytes) -> None:
    with pytest.raises(vault.VaultError):
        vault.unwrap_key(wrapped, b"salt", PASSWORD, user_id=1)
    assert not vault.key_matches(wrapped, vault.new_key(), user_id=1)


def test_key_matches() -> None:
    key = vault.new_key()
    _, wrapped = vault.wrap_key(key, PASSWORD, user_id=1)
    assert vault.key_matches(wrapped, key, user_id=1)
    assert not vault.key_matches(wrapped, vault.new_key(), user_id=1)
    assert not vault.key_matches(wrapped, key, user_id=2)


# ── Sealing secrets ──────────────────────────────────────────────────


def test_seal_round_trip_with_fresh_nonces() -> None:
    key = vault.new_key()
    secret = b'{"di_token": "abc"}'
    first = vault.seal(key, secret, user_id=1, provider="garmin")
    second = vault.seal(key, secret, user_id=1, provider="garmin")
    assert first != second  # a random nonce each time
    assert secret not in first
    for blob in (first, second):
        assert vault.unseal(key, blob, user_id=1, provider="garmin") == secret


@pytest.mark.parametrize(
    ("user_id", "provider", "use_other_key"),
    [(2, "garmin", False), (1, "strava", False), (1, "garmin", True)],
)
def test_sealed_secret_is_bound_to_key_user_and_provider(
    user_id: int, provider: str, use_other_key: bool
) -> None:
    key = vault.new_key()
    blob = vault.seal(key, b"secret", user_id=1, provider="garmin")
    with pytest.raises(vault.VaultError):
        vault.unseal(
            vault.new_key() if use_other_key else key,
            blob,
            user_id=user_id,
            provider=provider,
        )


@pytest.mark.parametrize("index", [0, 1, 13, -1])
def test_tampered_sealed_secret_does_not_open(index: int) -> None:
    key = vault.new_key()
    blob = bytearray(vault.seal(key, b"secret", user_id=1, provider="garmin"))
    blob[index] ^= 0x01
    with pytest.raises(vault.VaultError):
        vault.unseal(key, bytes(blob), user_id=1, provider="garmin")


@pytest.mark.parametrize("blob", [b"", b"\x01", b"\x01" + bytes(11)])
def test_malformed_sealed_secret(blob: bytes) -> None:
    with pytest.raises(vault.VaultError):
        vault.unseal(vault.new_key(), blob, user_id=1, provider="garmin")


def test_invalid_key_size_is_a_vault_error() -> None:
    with pytest.raises(vault.VaultError):
        vault.seal(b"short", b"secret", user_id=1, provider="garmin")
    with pytest.raises(vault.VaultError):
        vault.unseal(b"short", bytes(40), user_id=1, provider="garmin")


# ── Cookie encoding ──────────────────────────────────────────────────


def test_key_cookie_round_trip() -> None:
    key = vault.new_key()
    value = vault.encode_key(key)
    assert len(value) == 43 and "=" not in value
    assert vault.decode_key(value) == key


@pytest.mark.parametrize(
    "value", ["", "short", "!" * 43, "A" * 44, vault.encode_key(b"x" * 31) + "AAA"]
)
def test_bad_key_cookie(value: str) -> None:
    assert vault.decode_key(value) is None


# ── Accounts' vaults (users) ─────────────────────────────────────────


def _vault_columns(db_path: Path, user_id: int) -> tuple[bytes | None, bytes | None]:
    with db.connect(db_path) as conn:
        row = conn.execute(
            "SELECT vault_salt, vault_wrapped FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    return row["vault_salt"], row["vault_wrapped"]


def _store_tokens(db_path: Path, user_id: int, key: bytes) -> bytes:
    integrations_store.upsert_settings(
        db_path, user_id, "garmin", username="g@example.com", settings={}
    )
    sealed = vault.seal(key, b"tokens", user_id=user_id, provider="garmin")
    integrations_store.set_tokens(db_path, user_id, "garmin", sealed)
    return sealed


def _tokens(db_path: Path, user_id: int) -> bytes | None:
    integration = integrations_store.get(db_path, user_id, "garmin")
    assert integration is not None
    return integration.tokens_enc


def test_register_creates_a_vault_and_login_recovers_it(db_path: Path) -> None:
    registered = users.register(db_path, "alice", PASSWORD)
    salt, wrapped = _vault_columns(db_path, registered.user.id)
    assert salt is not None and wrapped is not None
    assert registered.vault_key not in wrapped

    login = users.login(db_path, "ALICE", PASSWORD)
    assert login is not None
    assert login.user == registered.user
    assert login.vault_key == registered.vault_key
    assert users.vault_key_matches(db_path, login.user.id, login.vault_key)
    # Kept out of reprs (and so out of tracebacks and debug logs).
    assert repr(login.vault_key) not in repr(login)


def test_create_user_also_creates_a_vault(db_path: Path) -> None:
    alice = users.create_user(db_path, "alice", PASSWORD)
    assert None not in _vault_columns(db_path, alice.id)


@pytest.mark.parametrize(
    ("username", "password"), [("alice", "wrong password"), ("nobody", PASSWORD)]
)
def test_login_failures(db_path: Path, username: str, password: str) -> None:
    users.create_user(db_path, "alice", PASSWORD)
    assert users.login(db_path, username, password) is None


def test_legacy_account_gets_a_vault_at_login(db_path: Path) -> None:
    alice = users.create_user(db_path, "alice", PASSWORD)
    with db.connect(db_path) as conn:
        conn.execute("UPDATE users SET vault_salt = NULL, vault_wrapped = NULL")
    login = users.login(db_path, "alice", PASSWORD)
    assert login is not None
    assert None not in _vault_columns(db_path, alice.id)
    again = users.login(db_path, "alice", PASSWORD)
    assert again is not None and again.vault_key == login.vault_key


def test_unusable_vault_is_replaced_and_its_secrets_forgotten(
    db_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    registered = users.register(db_path, "alice", PASSWORD)
    uid = registered.user.id
    _store_tokens(db_path, uid, registered.vault_key)
    # A vault wrapped under some other password (e.g. a password reset that
    # bypassed set_password).
    salt, wrapped = vault.wrap_key(vault.new_key(), "another password", user_id=uid)
    with db.connect(db_path) as conn:
        conn.execute(
            "UPDATE users SET vault_salt = ?, vault_wrapped = ? WHERE id = ?",
            (salt, wrapped, uid),
        )
    login = users.login(db_path, "alice", PASSWORD)
    assert login is not None
    assert login.vault_key != registered.vault_key
    assert users.vault_key_matches(db_path, uid, login.vault_key)
    assert _tokens(db_path, uid) is None
    assert "replacing it" in caplog.text


def test_set_password_with_the_key_rewraps_the_vault(db_path: Path) -> None:
    registered = users.register(db_path, "alice", PASSWORD)
    uid = registered.user.id
    sealed = _store_tokens(db_path, uid, registered.vault_key)
    users.set_password(db_path, uid, "new password!", vault_key=registered.vault_key)

    login = users.login(db_path, "alice", "new password!")
    assert login is not None
    assert login.vault_key == registered.vault_key
    assert _tokens(db_path, uid) == sealed
    assert users.login(db_path, "alice", PASSWORD) is None


def test_set_password_without_the_key_drops_the_vault(db_path: Path) -> None:
    registered = users.register(db_path, "alice", PASSWORD)
    uid = registered.user.id
    _store_tokens(db_path, uid, registered.vault_key)
    users.set_password(db_path, uid, "new password!")

    assert _vault_columns(db_path, uid) == (None, None)
    assert _tokens(db_path, uid) is None
    login = users.login(db_path, "alice", "new password!")
    assert login is not None and login.vault_key != registered.vault_key


def test_vault_key_matches(db_path: Path) -> None:
    alice = users.register(db_path, "alice", PASSWORD)
    bob = users.register(db_path, "bob", PASSWORD)
    assert users.vault_key_matches(db_path, alice.user.id, alice.vault_key)
    assert not users.vault_key_matches(db_path, alice.user.id, bob.vault_key)
    assert not users.vault_key_matches(db_path, 999, alice.vault_key)


def test_vault_key_is_never_stored_in_the_clear(db_path: Path) -> None:
    registered = users.register(db_path, "alice", PASSWORD)
    users.set_password(
        db_path, registered.user.id, "new password!", vault_key=registered.vault_key
    )
    for path in db_path.parent.glob("wishmap.db*"):
        data = path.read_bytes()
        assert registered.vault_key not in data
        assert PASSWORD.encode() not in data
