import os
import shutil
import subprocess
from collections.abc import Generator
from pathlib import Path

import pytest


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "wishmap.db"


# ── gpg-backed fixtures ───────────────────────────────────────────────
#
# Tests that exercise wishmap.secrets need a real GPG keypair with a known
# passphrase. We generate one in a per-test temporary GNUPGHOME so we don't
# touch the user's keyring. Real gpg-agent behavior is what the classifier
# is testing — mocking subprocess would test the mock, not gpg version drift.


TEST_PASSPHRASE = "test-passphrase-1234"
TEST_KEY_EMAIL = "wishmap-test@example.invalid"


def _gpg_available() -> bool:
    return shutil.which("gpg") is not None


pytest.importorskip  # ensure pytest namespace, no-op


@pytest.fixture
def gpg_homedir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[Path, None, None]:
    """Provide an isolated GNUPGHOME with one test keypair generated.

    The generated key encrypts the fixture pass entry. Tests can derive
    the matching .gpg file via the password_store fixture.

    Skips the test if gpg is not on PATH.
    """
    if not _gpg_available():
        pytest.skip("gpg binary not available on PATH")

    homedir = tmp_path / "gnupg"
    homedir.mkdir()
    homedir.chmod(0o700)
    monkeypatch.setenv("GNUPGHOME", str(homedir))

    # Allow loopback so warm_gpg_agent tests can drive a real decrypt.
    (homedir / "gpg-agent.conf").write_text("allow-loopback-pinentry\n")

    batch = tmp_path / "key.batch"
    batch.write_text(
        f"""%echo Generating test key
Key-Type: EDDSA
Key-Curve: ed25519
Subkey-Type: ECDH
Subkey-Curve: cv25519
Name-Real: Wishmap Test
Name-Email: {TEST_KEY_EMAIL}
Expire-Date: 0
Passphrase: {TEST_PASSPHRASE}
%commit
%echo done
"""
    )
    subprocess.run(
        ["gpg", "--batch", "--pinentry-mode", "loopback", "--gen-key", str(batch)],
        check=True,
        capture_output=True,
    )

    try:
        yield homedir
    finally:
        # Kill the agent so subsequent tests don't share its cache.
        subprocess.run(
            ["gpgconf", "--kill", "gpg-agent"],
            env={**os.environ, "GNUPGHOME": str(homedir)},
            capture_output=True,
        )


@pytest.fixture
def password_store(
    gpg_homedir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Provide a PASSWORD_STORE_DIR with one entry 'wishmap/test'.

    The entry is encrypted to the gpg_homedir keypair with TEST_PASSPHRASE.
    Returns the password store directory (the .gpg file lives at
    <store>/wishmap/test.gpg).
    """
    store = tmp_path / "password-store"
    (store / "wishmap").mkdir(parents=True)
    (store / ".gpg-id").write_text(TEST_KEY_EMAIL + "\n")
    monkeypatch.setenv("PASSWORD_STORE_DIR", str(store))

    # Encrypt a known plaintext for the entry. The first line of the
    # plaintext is what pass treats as "the password".
    plaintext = "the-actual-secret-value\nextra-metadata-line\n"
    subprocess.run(
        [
            "gpg",
            "--batch",
            "--yes",
            "--trust-model", "always",
            "--recipient", TEST_KEY_EMAIL,
            "--encrypt",
            "--output", str(store / "wishmap" / "test.gpg"),
        ],
        input=plaintext,
        text=True,
        check=True,
        capture_output=True,
    )
    return store


@pytest.fixture
def cold_agent(gpg_homedir: Path) -> None:
    """Ensure gpg-agent's cache is empty before a test runs."""
    subprocess.run(
        ["gpgconf", "--kill", "gpg-agent"],
        env={**os.environ, "GNUPGHOME": str(gpg_homedir)},
        capture_output=True,
    )
