"""Tests for the password/secret resolution helpers in strava and garmin.

These wrappers delegate to wishmap.secrets.resolve_secret. We check that
the wrappers pass the right fields through and that secrets.resolve_secret's
exception hierarchy reaches the caller intact.

Deep coverage of secrets.resolve_secret itself (real gpg fixtures, status-fd
classifier) lives in tests/test_secrets.py.
"""

import subprocess
from typing import Any

import pytest

from wishmap import garmin, secrets, strava
from wishmap.exceptions import PassphraseRequiredError, SyncError
from wishmap.models import GarminConfig, StravaConfig


def _fake_pass_show_locked_agent(
    cmd: list[str], **kwargs: Any
) -> subprocess.CompletedProcess[str]:
    """Simulate `pass show` against a gpg-agent with no cached key
    and no usable pinentry — the headless-service failure mode."""
    return subprocess.CompletedProcess(
        args=cmd,
        returncode=2,
        stdout="",
        stderr=(
            "[GNUPG:] ENC_TO DDD603F96A41354E 18 0\n"
            "[GNUPG:] KEY_CONSIDERED AA5FC6D70B00E4F4B32624313AA7961C76E3657E 0\n"
            "[GNUPG:] NEED_PASSPHRASE DDD603F96A41354E 3AA7961C76E3657E 18 0\n"
            "[GNUPG:] INQUIRE_MAXLEN 100\n"
            "gpg: Sorry, we are in batchmode - can't get input\n"
        ),
    )


def _fake_pass_show_ok(
    cmd: list[str], **kwargs: Any
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=cmd,
        returncode=0,
        stdout="my-secret-value\n",
        stderr=(
            "[GNUPG:] KEY_CONSIDERED AA5FC6D70B00E4F4B32624313AA7961C76E3657E 0\n"
            "[GNUPG:] DECRYPTION_KEY ABC 123 u\n"
            "[GNUPG:] DECRYPTION_OKAY\n"
        ),
    )


def _fake_run_raising_file_not_found(
    cmd: list[str], **kwargs: Any
) -> subprocess.CompletedProcess[str]:
    raise FileNotFoundError(2, "No such file or directory", "pass")


# ── Strava ──


def test_strava_get_client_secret_returns_decoded_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = StravaConfig(client_id="x", client_secret_pass="wishmap/strava")
    monkeypatch.setattr("wishmap.secrets.subprocess.run", _fake_pass_show_ok)
    assert strava._get_client_secret(cfg) == "my-secret-value"


def test_strava_get_client_secret_surfaces_passphrase_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When gpg-agent has no cached key, the web UI needs to prompt —
    surfaced as PassphraseRequiredError (a SyncError subclass)."""
    cfg = StravaConfig(client_id="x", client_secret_pass="wishmap/strava")
    monkeypatch.setattr(
        "wishmap.secrets.subprocess.run", _fake_pass_show_locked_agent
    )
    with pytest.raises(PassphraseRequiredError) as exc:
        strava._get_client_secret(cfg)
    assert "wishmap/strava" in str(exc.value)


def test_strava_get_client_secret_wraps_missing_pass_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = StravaConfig(client_id="x", client_secret_pass="wishmap/strava")
    monkeypatch.setattr(
        "wishmap.secrets.subprocess.run", _fake_run_raising_file_not_found
    )
    with pytest.raises(SyncError) as exc:
        strava._get_client_secret(cfg)
    assert "'pass' is not on PATH" in str(exc.value)


def test_strava_get_client_secret_no_config_raises_sync_error() -> None:
    """Empty config (no client_secret, no file, no pass) is a SyncError —
    sync_runner only catches SyncError specially."""
    cfg = StravaConfig(client_id="x")
    with pytest.raises(SyncError):
        strava._get_client_secret(cfg)


# ── Garmin ──


def test_garmin_get_password_returns_decoded_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = GarminConfig(username="u", password_pass="wishmap/garmin")
    monkeypatch.setattr("wishmap.secrets.subprocess.run", _fake_pass_show_ok)
    assert garmin._get_password(cfg) == "my-secret-value"


def test_garmin_get_password_surfaces_passphrase_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = GarminConfig(username="u", password_pass="wishmap/garmin")
    monkeypatch.setattr(
        "wishmap.secrets.subprocess.run", _fake_pass_show_locked_agent
    )
    with pytest.raises(PassphraseRequiredError) as exc:
        garmin._get_password(cfg)
    assert "wishmap/garmin" in str(exc.value)


def test_garmin_get_password_wraps_missing_pass_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = GarminConfig(username="u", password_pass="wishmap/garmin")
    monkeypatch.setattr(
        "wishmap.secrets.subprocess.run", _fake_run_raising_file_not_found
    )
    with pytest.raises(SyncError) as exc:
        garmin._get_password(cfg)
    assert "'pass' is not on PATH" in str(exc.value)


def test_garmin_get_password_no_config_raises_sync_error() -> None:
    cfg = GarminConfig(username="u")
    with pytest.raises(SyncError):
        garmin._get_password(cfg)


# ── Direct value and file paths (regression — must behave identically
#     to the pre-refactor implementation) ──


def test_resolve_secret_direct_value_wins() -> None:
    assert (
        secrets.resolve_secret(
            direct="literal", file_path="", pass_entry="", label="x"
        )
        == "literal"
    )


def test_resolve_secret_file_path_reads_and_strips(tmp_path: Any) -> None:
    p = tmp_path / "secret.txt"
    p.write_text("  value-from-file  \n")
    assert (
        secrets.resolve_secret(
            direct="", file_path=str(p), pass_entry="", label="x"
        )
        == "value-from-file"
    )


def test_resolve_secret_precedence_direct_over_file_over_pass(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If direct is set, file and pass are not consulted."""
    monkeypatch.setattr(
        "wishmap.secrets.subprocess.run", _fake_run_raising_file_not_found
    )
    p = tmp_path / "secret.txt"
    p.write_text("from-file")
    assert (
        secrets.resolve_secret(
            direct="from-direct",
            file_path=str(p),
            pass_entry="some/entry",
            label="x",
        )
        == "from-direct"
    )
