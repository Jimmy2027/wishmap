"""Per-user secrets vault: AES-256-GCM under a key only the password unlocks.

Every account has a random 32-byte *vault key*. The DB stores it only
wrapped (encrypted) under a key-encryption key derived from the user's
password with scrypt (`users.vault_salt` / `users.vault_wrapped`). Logging
in unwraps it and hands it to the browser in the HttpOnly `wishmap_vault`
cookie (see `auth`); the server keeps it in the clear only while a request
that carries the cookie is being handled. Any device that logs in recovers
the same key, and a password change merely re-wraps it.

The wrapped blob also carries a short HMAC of the key (`key_matches`), so
the server can tell whether a cookie holds the account's *current* vault
key — without the password, and without that check revealing anything
about the 256-bit key.

Provider secrets (Garmin's session tokens) are `seal`ed with the vault key
before they reach the DB. Each sealed blob is bound (as AES-GCM associated
data) to the user and provider it belongs to, so a blob copied onto
another account's or provider's row fails to open rather than decrypting
into the wrong place.

Threat model, in short: a stolen DB alone yields nothing without
brute-forcing a user's password through scrypt; a compromised *running*
server sees the keys of requests it handles. Forgetting the password loses
the vault (the provider has to be logged into again), never the account.

This module is a leaf (stdlib + `cryptography`), so `users` can depend on it.
"""

import base64
import binascii
import hashlib
import hmac
import secrets
import threading

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_BYTES = 32  # AES-256
SALT_BYTES = 16
NONCE_BYTES = 12  # AES-GCM's standard nonce size

# scrypt cost of the key-encryption key: N=2^15, r=8, p=1 (~60 ms, 32 MiB),
# the same as the password hash. Module constants so tests can lower them;
# unwrapping always uses the parameters stored in the wrapped blob.
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
_SCRYPT_MAXMEM = 64 * 1024 * 1024
# Highest log2(N) accepted from a stored blob (bounds memory on a bad row).
_MAX_LOG2_N = 20
# Each derivation allocates tens of MiB; cap how many run at once.
_KDF_SLOTS = threading.BoundedSemaphore(4)

# Blob layouts (integers one byte each; headers are authenticated too):
#   wrapped key: version | log2(N) | r | p | check | nonce | AES-GCM(KEK, key)
#   sealed data: version | nonce | AES-GCM(vault key, plaintext)
# where check = HMAC-SHA256(key, "…check|user:<id>")[:16].
_WRAP_VERSION = 1
_SEAL_VERSION = 1
_CHECK_BYTES = 16
_WRAP_HEADER_BYTES = 4 + _CHECK_BYTES
_SEAL_HEADER_BYTES = 1


class VaultError(Exception):
    """A vault blob did not open: wrong key or password, tampered, or malformed."""


def new_key() -> bytes:
    """A fresh random vault key."""
    return secrets.token_bytes(KEY_BYTES)


def _kek(password: str, salt: bytes, log2_n: int, r: int, p: int) -> bytes:
    """Derive the key-encryption key from the password (scrypt)."""
    with _KDF_SLOTS:
        return hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=2**log2_n,
            r=r,
            p=p,
            maxmem=_SCRYPT_MAXMEM,
            dklen=KEY_BYTES,
        )


def _key_check(key: bytes, user_id: int) -> bytes:
    message = f"wishmap-vault-check|user:{user_id}".encode()
    return hmac.new(key, message, hashlib.sha256).digest()[:_CHECK_BYTES]


def _wrap_aad(header: bytes, user_id: int) -> bytes:
    return header + f"|wishmap-vault-key|user:{user_id}".encode()


def _seal_aad(header: bytes, user_id: int, provider: str) -> bytes:
    return header + f"|wishmap-secret|user:{user_id}|provider:{provider}".encode()


def wrap_key(key: bytes, password: str, *, user_id: int) -> tuple[bytes, bytes]:
    """Encrypt the vault key under `password`; return `(salt, wrapped)` for the DB."""
    if len(key) != KEY_BYTES:
        raise ValueError("vault keys are 32 bytes")
    log2_n = SCRYPT_N.bit_length() - 1
    if 2**log2_n != SCRYPT_N:
        raise ValueError("SCRYPT_N must be a power of two")
    salt = secrets.token_bytes(SALT_BYTES)
    header = bytes((_WRAP_VERSION, log2_n, SCRYPT_R, SCRYPT_P)) + _key_check(
        key, user_id
    )
    nonce = secrets.token_bytes(NONCE_BYTES)
    kek = _kek(password, salt, log2_n, SCRYPT_R, SCRYPT_P)
    ciphertext = AESGCM(kek).encrypt(nonce, key, _wrap_aad(header, user_id))
    return salt, header + nonce + ciphertext


def unwrap_key(wrapped: bytes, salt: bytes, password: str, *, user_id: int) -> bytes:
    """Recover the vault key from `wrap_key`'s output; VaultError if it won't open."""
    header = wrapped[:_WRAP_HEADER_BYTES]
    nonce = wrapped[_WRAP_HEADER_BYTES : _WRAP_HEADER_BYTES + NONCE_BYTES]
    ciphertext = wrapped[_WRAP_HEADER_BYTES + NONCE_BYTES :]
    if len(header) != _WRAP_HEADER_BYTES or header[0] != _WRAP_VERSION:
        raise VaultError("unknown wrapped-key format")
    log2_n, r, p = header[1:4]
    if not 1 <= log2_n <= _MAX_LOG2_N or r < 1 or p < 1:
        raise VaultError("wrapped key has invalid scrypt parameters")
    try:
        kek = _kek(password, salt, log2_n, r, p)
        key = AESGCM(kek).decrypt(nonce, ciphertext, _wrap_aad(header, user_id))
    except (InvalidTag, ValueError) as e:
        raise VaultError("the vault key did not unwrap") from e
    if len(key) != KEY_BYTES:
        raise VaultError("the unwrapped vault key has the wrong length")
    return key


def key_matches(wrapped: bytes, key: bytes, *, user_id: int) -> bool:
    """True if `key` is the vault key `wrapped` holds (constant-time check)."""
    if len(wrapped) < _WRAP_HEADER_BYTES or wrapped[0] != _WRAP_VERSION:
        return False
    return hmac.compare_digest(wrapped[4:_WRAP_HEADER_BYTES], _key_check(key, user_id))


def seal(key: bytes, plaintext: bytes, *, user_id: int, provider: str) -> bytes:
    """Encrypt a secret of `user_id` for `provider` under the vault key.

    A fresh random nonce per call, so sealing the same secret twice gives
    different blobs.
    """
    header = bytes((_SEAL_VERSION,))
    nonce = secrets.token_bytes(NONCE_BYTES)
    try:
        ciphertext = AESGCM(key).encrypt(
            nonce, plaintext, _seal_aad(header, user_id, provider)
        )
    except ValueError as e:  # a key of the wrong size
        raise VaultError("invalid vault key") from e
    return header + nonce + ciphertext


def unseal(key: bytes, blob: bytes, *, user_id: int, provider: str) -> bytes:
    """Decrypt a `seal`ed secret; VaultError for a wrong key, user, provider or blob."""
    header = blob[:_SEAL_HEADER_BYTES]
    nonce = blob[_SEAL_HEADER_BYTES : _SEAL_HEADER_BYTES + NONCE_BYTES]
    ciphertext = blob[_SEAL_HEADER_BYTES + NONCE_BYTES :]
    if header != bytes((_SEAL_VERSION,)) or len(nonce) != NONCE_BYTES:
        raise VaultError("unknown sealed-secret format")
    try:
        return AESGCM(key).decrypt(
            nonce, ciphertext, _seal_aad(header, user_id, provider)
        )
    except (InvalidTag, ValueError) as e:
        raise VaultError("the secret did not open with this vault key") from e


def encode_key(key: bytes) -> str:
    """The vault key as a cookie value (URL-safe base64, no padding)."""
    return base64.urlsafe_b64encode(key).decode("ascii").rstrip("=")


def decode_key(value: str) -> bytes | None:
    """Parse `encode_key`'s output; None if it isn't a well-formed vault key."""
    # 32 bytes -> 43 characters; don't decode arbitrary junk.
    if len(value) != 43:
        return None
    try:
        key = base64.urlsafe_b64decode(value + "=")
    except (binascii.Error, ValueError):
        return None
    return key if len(key) == KEY_BYTES else None
