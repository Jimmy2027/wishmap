"""Tests for wishmap.secrets — the gpg/pass classifier and helpers.

These tests use a real ephemeral GPG keypair (see conftest.gpg_homedir +
password_store fixtures). The classifier's exit-on-keyword logic is the
load-bearing part of the module — mocks would not catch gpg version drift.
"""

import os
import subprocess
from pathlib import Path

import pytest

from wishmap import secrets
from wishmap.exceptions import (
    BadPassphraseError,
    LoopbackDisabledError,
    PassphraseRequiredError,
    SyncError,
)
from tests.conftest import TEST_PASSPHRASE


# ── _classify_gpg_outcome (unit) ─────────────────────────────────────


def test_classify_ok_on_decryption_okay() -> None:
    assert (
        secrets._classify_gpg_outcome(
            0, "[GNUPG:] DECRYPTION_KEY ABC 123 u\n[GNUPG:] DECRYPTION_OKAY\n"
        )
        == secrets._Outcome.OK
    )


def test_classify_bad_passphrase() -> None:
    assert (
        secrets._classify_gpg_outcome(
            2, "gpg: decryption failed: Bad passphrase\n"
        )
        == secrets._Outcome.BAD_PASSPHRASE
    )


def test_classify_loopback_disabled() -> None:
    assert (
        secrets._classify_gpg_outcome(
            2,
            "gpg: setting pinentry mode 'loopback' failed: Not supported\n"
            "[GNUPG:] ERROR set_pinentry_mode 67108924\n",
        )
        == secrets._Outcome.LOOPBACK_DISABLED
    )


def test_classify_passphrase_required_via_need_passphrase() -> None:
    assert (
        secrets._classify_gpg_outcome(
            2,
            "[GNUPG:] NEED_PASSPHRASE DDD 3AA 18 0\n"
            "[GNUPG:] INQUIRE_MAXLEN 100\n"
            "gpg: Sorry, we are in batchmode - can't get input\n",
        )
        == secrets._Outcome.PASSPHRASE_REQUIRED
    )


def test_classify_passphrase_required_via_pinentry_launched() -> None:
    assert (
        secrets._classify_gpg_outcome(
            2,
            "[GNUPG:] PINENTRY_LAUNCHED 3743 gnome3:curses 1.3.2 /dev/null\n"
            "[GNUPG:] NO_SECKEY DDD\n"
            "gpg: public key decryption failed: Timeout\n",
        )
        == secrets._Outcome.PASSPHRASE_REQUIRED
    )


def test_classify_agent_unreachable_on_unknown_failure() -> None:
    assert (
        secrets._classify_gpg_outcome(2, "some random unparsed gpg error\n")
        == secrets._Outcome.AGENT_UNREACHABLE
    )


# ── warm_gpg_agent against a real keypair ─────────────────────────────


def test_warm_gpg_agent_happy_path(password_store: Path, cold_agent: None) -> None:
    """Correct passphrase loopback-decrypts the fixture entry. After this
    runs, gpg-agent has the key cached."""
    secrets.warm_gpg_agent("wishmap/test", TEST_PASSPHRASE)


def test_warm_gpg_agent_caches_for_subsequent_reads(
    password_store: Path, cold_agent: None
) -> None:
    """The premise of the whole design — warm once, read many."""
    secrets.warm_gpg_agent("wishmap/test", TEST_PASSPHRASE)
    # Now resolve_secret via pass show should succeed without the
    # passphrase, because gpg-agent has the key.
    value = secrets._resolve_via_pass("wishmap/test", "Test")
    assert value == "the-actual-secret-value"


def test_warm_gpg_agent_bad_passphrase(
    password_store: Path, cold_agent: None
) -> None:
    with pytest.raises(BadPassphraseError):
        secrets.warm_gpg_agent("wishmap/test", "wrong-passphrase")


def test_warm_gpg_agent_loopback_disabled(
    password_store: Path, gpg_homedir: Path, cold_agent: None
) -> None:
    """Flip gpg-agent.conf to explicitly reject loopback."""
    (gpg_homedir / "gpg-agent.conf").write_text("no-allow-loopback-pinentry\n")
    subprocess.run(
        ["gpgconf", "--kill", "gpg-agent"],
        env={**os.environ, "GNUPGHOME": str(gpg_homedir)},
        capture_output=True,
    )
    with pytest.raises(LoopbackDisabledError):
        secrets.warm_gpg_agent("wishmap/test", TEST_PASSPHRASE)


def test_warm_gpg_agent_entry_not_found(password_store: Path) -> None:
    with pytest.raises(SyncError) as exc:
        secrets.warm_gpg_agent("does/not/exist", TEST_PASSPHRASE)
    assert "does/not/exist" in str(exc.value)


def test_warm_gpg_agent_does_not_leak_passphrase_in_exception(
    password_store: Path, cold_agent: None
) -> None:
    """CQ3 invariant: the passphrase string never appears in any raised
    exception's message or args. Use a unique sentinel."""
    sentinel = "DELIBERATELY_WRONG_xyzzy_99887766"
    with pytest.raises(BadPassphraseError) as exc:
        secrets.warm_gpg_agent("wishmap/test", sentinel)
    # Combined string of message + all args + repr.
    combined = " ".join(
        [str(exc.value), repr(exc.value), str(exc.value.args)]
    )
    assert sentinel not in combined


# ── resolve_secret (the read path) ────────────────────────────────────


def test_resolve_secret_via_pass_returns_first_line(
    password_store: Path, cold_agent: None
) -> None:
    """Warm the agent first, then read — the design's happy path."""
    secrets.warm_gpg_agent("wishmap/test", TEST_PASSPHRASE)
    value = secrets.resolve_secret(
        direct="", file_path="", pass_entry="wishmap/test", label="Test"
    )
    assert value == "the-actual-secret-value"


def test_resolve_secret_locked_agent_raises_passphrase_required(
    password_store: Path, cold_agent: None
) -> None:
    """With no cached key and no usable pinentry, the read path must
    surface PassphraseRequiredError so the web UI can prompt."""
    with pytest.raises(PassphraseRequiredError) as exc:
        secrets.resolve_secret(
            direct="", file_path="", pass_entry="wishmap/test", label="Test"
        )
    assert "wishmap/test" in str(exc.value)


def test_resolve_secret_password_store_dir_gpg_opts_preserved(
    password_store: Path, cold_agent: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: if the user has PASSWORD_STORE_GPG_OPTS already set,
    wishmap's --batch --status-fd=2 injection must APPEND, not overwrite."""
    secrets.warm_gpg_agent("wishmap/test", TEST_PASSPHRASE)
    monkeypatch.setenv("PASSWORD_STORE_GPG_OPTS", "--no-tty")
    # Should still succeed — --no-tty is harmless and we appended our flags.
    value = secrets.resolve_secret(
        direct="", file_path="", pass_entry="wishmap/test", label="Test"
    )
    assert value == "the-actual-secret-value"


def test_resolve_secret_empty_pass_entry_raises_sync_error() -> None:
    """All three inputs empty → SyncError with helpful message."""
    with pytest.raises(SyncError) as exc:
        secrets.resolve_secret(direct="", file_path="", pass_entry="", label="X")
    assert "X" in str(exc.value)


def test_resolve_secret_direct_value_does_not_invoke_gpg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If direct is set, the subprocess should never run."""
    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("subprocess.run should not have been called")
    monkeypatch.setattr("wishmap.secrets.subprocess.run", boom)
    assert (
        secrets.resolve_secret(
            direct="literal", file_path="", pass_entry="some/entry", label="X"
        )
        == "literal"
    )


# ── Status-line regex parses real gpg output ──────────────────────────


def test_parse_status_extracts_all_keywords() -> None:
    sample = (
        "[GNUPG:] ENC_TO DDD603F96A41354E 18 0\n"
        "[GNUPG:] KEY_CONSIDERED AA5FC6D7 0\n"
        "[GNUPG:] DECRYPTION_KEY 3FBB 123 u\n"
        "[GNUPG:] BEGIN_DECRYPTION\n"
        "[GNUPG:] DECRYPTION_OKAY\n"
        "[GNUPG:] END_DECRYPTION\n"
    )
    keywords = secrets._parse_status(sample)
    assert "ENC_TO" in keywords
    assert "KEY_CONSIDERED" in keywords
    assert "DECRYPTION_OKAY" in keywords
    assert "BEGIN_DECRYPTION" in keywords
