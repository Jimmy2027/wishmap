"""Tests for the password/secret resolution helpers in strava and garmin.

These functions shell out to `pass` (passwordstore.org) when configured
to do so. We need to make sure pass-related failures surface as readable
SyncErrors rather than raw CalledProcessError, especially when wishmap
runs as a background service detached from a TTY (gpg-agent can't prompt).
"""

import subprocess
from typing import Any

import pytest

from wishmap import garmin, strava
from wishmap.exceptions import SyncError
from wishmap.models import GarminConfig, StravaConfig


def _fake_run_raising_called_process_error(
    cmd: list[str], **kwargs: Any
) -> subprocess.CompletedProcess[str]:
    raise subprocess.CalledProcessError(
        returncode=2,
        cmd=cmd,
        output="",
        stderr="gpg: decryption failed: No secret key",
    )


def _fake_run_raising_file_not_found(
    cmd: list[str], **kwargs: Any
) -> subprocess.CompletedProcess[str]:
    raise FileNotFoundError(2, "No such file or directory", "pass")


# ── Strava ──


def test_strava_get_client_secret_wraps_pass_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = StravaConfig(client_id="x", client_secret_pass="strava_secret")
    monkeypatch.setattr(
        "wishmap.strava.subprocess.run", _fake_run_raising_called_process_error
    )
    with pytest.raises(SyncError) as exc:
        strava._get_client_secret(cfg)
    msg = str(exc.value)
    assert "strava_secret" in msg
    assert "gpg: decryption failed" in msg
    assert "gpg-agent" in msg


def test_strava_get_client_secret_wraps_missing_pass_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = StravaConfig(client_id="x", client_secret_pass="strava_secret")
    monkeypatch.setattr(
        "wishmap.strava.subprocess.run", _fake_run_raising_file_not_found
    )
    with pytest.raises(SyncError) as exc:
        strava._get_client_secret(cfg)
    assert "'pass' is not on PATH" in str(exc.value)


def test_strava_get_client_secret_no_config_raises_sync_error() -> None:
    """Empty config (no client_secret, no file, no pass) is a SyncError now,
    not a ValueError — sync_runner only catches SyncError specially."""
    cfg = StravaConfig(client_id="x")
    with pytest.raises(SyncError):
        strava._get_client_secret(cfg)


# ── Garmin ──


def test_garmin_get_password_wraps_pass_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = GarminConfig(username="u", password_pass="garmin_pw")
    monkeypatch.setattr(
        "wishmap.garmin.subprocess.run", _fake_run_raising_called_process_error
    )
    with pytest.raises(SyncError) as exc:
        garmin._get_password(cfg)
    msg = str(exc.value)
    assert "garmin_pw" in msg
    assert "gpg: decryption failed" in msg
    assert "gpg-agent" in msg


def test_garmin_get_password_wraps_missing_pass_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = GarminConfig(username="u", password_pass="garmin_pw")
    monkeypatch.setattr(
        "wishmap.garmin.subprocess.run", _fake_run_raising_file_not_found
    )
    with pytest.raises(SyncError) as exc:
        garmin._get_password(cfg)
    assert "'pass' is not on PATH" in str(exc.value)


def test_garmin_get_password_no_config_raises_sync_error() -> None:
    cfg = GarminConfig(username="u")
    with pytest.raises(SyncError):
        garmin._get_password(cfg)
