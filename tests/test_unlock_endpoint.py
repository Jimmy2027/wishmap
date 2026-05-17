"""HTTP integration tests for POST /api/unlock.

Endpoint contract:
  - 204 on success (every configured pass entry warmed)
  - 400 when no pass entries are configured
  - 401 bad_passphrase (entry name included in response)
  - 403 cross-origin
  - 503 loopback_disabled / agent_unreachable
  - 500 generic
  - log redaction: passphrase never appears in captured logs

The warm_gpg_agent helper is mocked in most tests so we focus on the
endpoint's HTTP contract, not gpg behavior (which is covered by
test_secrets.py against a real keypair).
"""

import importlib
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from wishmap import sync_runner
from wishmap.exceptions import (
    AgentUnreachableError,
    BadPassphraseError,
    LoopbackDisabledError,
    SyncError,
)

MINIMAL_GPX = """<?xml version="1.0"?>
<gpx version="1.1" creator="test" xmlns="http://www.topografix.com/GPX/1/1">
  <trk><name>t</name><trkseg>
    <trkpt lat="46.9" lon="9.7"/>
    <trkpt lat="46.91" lon="9.71"/>
  </trkseg></trk>
</gpx>
"""


def _write_config(tmp_path: Path, body: str) -> None:
    gpx = tmp_path / "route.gpx"
    gpx.write_text(MINIMAL_GPX)
    (tmp_path / "wishmap.toml").write_text(body)


def _make_client_iter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    monkeypatch.setenv("WISHMAP_CONFIG", str(tmp_path / "wishmap.toml"))
    from wishmap import app as app_module

    importlib.reload(app_module)
    sync_runner.reset_status_for_tests()
    # `with TestClient` triggers FastAPI lifespan startup, which is what
    # initializes module-level _config, _base_path, etc.
    with TestClient(app_module.app) as c:
        yield c


@pytest.fixture
def client_with_pass_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    _write_config(
        tmp_path,
        'title = "test"\n'
        "\n"
        "[strava]\n"
        'client_id = "x"\n'
        'client_secret_pass = "wishmap/strava"\n'
        "\n"
        "[garmin]\n"
        'username = "u"\n'
        'password_pass = "wishmap/garmin"\n'
        "\n"
        "[[routes]]\n"
        'id = "r1"\n'
        'name = "Route One"\n'
        'sport = "hiking"\n'
        'status = "planned"\n'
        'gpx = "route.gpx"\n',
    )
    yield from _make_client_iter(tmp_path, monkeypatch)


@pytest.fixture
def client_no_pass_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    _write_config(
        tmp_path,
        'title = "test"\n'
        "\n"
        "[strava]\n"
        'client_id = "x"\n'
        'client_secret = "literal"\n'
        "\n"
        "[garmin]\n"
        'username = "u"\n'
        'password = "literal"\n'
        "\n"
        "[[routes]]\n"
        'id = "r1"\n'
        'name = "Route One"\n'
        'sport = "hiking"\n'
        'status = "planned"\n'
        'gpx = "route.gpx"\n',
    )
    yield from _make_client_iter(tmp_path, monkeypatch)


# ── happy path ────────────────────────────────────────────────────────


def test_unlock_returns_204_on_success(
    client_with_pass_entries: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str]] = []

    def fake_warm(entry: str, passphrase: str) -> None:
        calls.append((entry, passphrase))

    monkeypatch.setattr("wishmap.secrets.warm_gpg_agent", fake_warm)
    resp = client_with_pass_entries.post(
        "/api/unlock",
        json={"passphrase": "correct"},
        headers={"Sec-Fetch-Site": "same-origin"},
    )
    assert resp.status_code == 204
    assert resp.content == b""
    # Both configured entries should be warmed in order.
    entries = [c[0] for c in calls]
    assert entries == ["wishmap/strava", "wishmap/garmin"]


def test_unlock_dedupes_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If garmin and strava reference the same pass entry (uncommon but
    legal), warm only once."""
    _write_config(
        tmp_path,
        'title = "test"\n'
        "\n"
        "[strava]\n"
        'client_id = "x"\n'
        'client_secret_pass = "shared/entry"\n'
        "\n"
        "[garmin]\n"
        'username = "u"\n'
        'password_pass = "shared/entry"\n'
        "\n"
        "[[routes]]\n"
        'id = "r1"\n'
        'name = "Route One"\n'
        'sport = "hiking"\n'
        'status = "planned"\n'
        'gpx = "route.gpx"\n',
    )
    calls: list[str] = []
    monkeypatch.setattr(
        "wishmap.secrets.warm_gpg_agent",
        lambda entry, _p: calls.append(entry),
    )
    for client in _make_client_iter(tmp_path, monkeypatch):
        resp = client.post(
            "/api/unlock",
            json={"passphrase": "x"},
            headers={"Sec-Fetch-Site": "same-origin"},
        )
        assert resp.status_code == 204
        assert calls == ["shared/entry"]


def test_unlock_explicit_warm_pass_entry_takes_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(
        tmp_path,
        'title = "test"\n'
        'warm_pass_entry = "smoke/test"\n'
        "\n"
        "[strava]\n"
        'client_id = "x"\n'
        'client_secret_pass = "wishmap/strava"\n'
        "\n"
        "[[routes]]\n"
        'id = "r1"\n'
        'name = "Route One"\n'
        'sport = "hiking"\n'
        'status = "planned"\n'
        'gpx = "route.gpx"\n',
    )
    calls: list[str] = []
    monkeypatch.setattr(
        "wishmap.secrets.warm_gpg_agent",
        lambda entry, _p: calls.append(entry),
    )
    for client in _make_client_iter(tmp_path, monkeypatch):
        resp = client.post(
            "/api/unlock",
            json={"passphrase": "x"},
            headers={"Sec-Fetch-Site": "same-origin"},
        )
        assert resp.status_code == 204
        assert calls[0] == "smoke/test"  # warm_pass_entry first


# ── error paths ───────────────────────────────────────────────────────


def test_unlock_returns_401_on_bad_passphrase(
    client_with_pass_entries: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_warm(entry: str, _p: str) -> None:
        raise BadPassphraseError(f"Wrong passphrase for pass entry '{entry}'")

    monkeypatch.setattr("wishmap.secrets.warm_gpg_agent", fake_warm)
    resp = client_with_pass_entries.post(
        "/api/unlock",
        json={"passphrase": "wrong"},
        headers={"Sec-Fetch-Site": "same-origin"},
    )
    assert resp.status_code == 401
    body = resp.json()
    assert body["reason"] == "bad_passphrase"
    # The entry name is included so multi-key users can see which one rejected.
    assert body["entry"] == "wishmap/strava"


def test_unlock_returns_401_on_second_entry_with_different_key(
    client_with_pass_entries: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Multi-key reality (A3): first entry succeeds, second rejects."""
    state = {"calls": 0}

    def fake_warm(entry: str, _p: str) -> None:
        state["calls"] += 1
        if state["calls"] == 1:
            return  # strava OK
        raise BadPassphraseError(f"Wrong passphrase for pass entry '{entry}'")

    monkeypatch.setattr("wishmap.secrets.warm_gpg_agent", fake_warm)
    resp = client_with_pass_entries.post(
        "/api/unlock",
        json={"passphrase": "matches-strava-only"},
        headers={"Sec-Fetch-Site": "same-origin"},
    )
    assert resp.status_code == 401
    assert resp.json()["entry"] == "wishmap/garmin"


def test_unlock_returns_503_loopback_disabled(
    client_with_pass_entries: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_warm(_entry: str, _p: str) -> None:
        raise LoopbackDisabledError("add allow-loopback-pinentry...")

    monkeypatch.setattr("wishmap.secrets.warm_gpg_agent", fake_warm)
    resp = client_with_pass_entries.post(
        "/api/unlock",
        json={"passphrase": "x"},
        headers={"Sec-Fetch-Site": "same-origin"},
    )
    assert resp.status_code == 503
    body = resp.json()
    assert body["reason"] == "loopback_disabled"
    assert "allow-loopback-pinentry" in body["hint"]


def test_unlock_returns_503_agent_unreachable(
    client_with_pass_entries: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_warm(_entry: str, _p: str) -> None:
        raise AgentUnreachableError("gpg-agent not running")

    monkeypatch.setattr("wishmap.secrets.warm_gpg_agent", fake_warm)
    resp = client_with_pass_entries.post(
        "/api/unlock",
        json={"passphrase": "x"},
        headers={"Sec-Fetch-Site": "same-origin"},
    )
    assert resp.status_code == 503
    assert resp.json()["reason"] == "agent_unreachable"


def test_unlock_returns_500_for_other_sync_error(
    client_with_pass_entries: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_warm(_entry: str, _p: str) -> None:
        raise SyncError("unspecified gpg failure")

    monkeypatch.setattr("wishmap.secrets.warm_gpg_agent", fake_warm)
    resp = client_with_pass_entries.post(
        "/api/unlock",
        json={"passphrase": "x"},
        headers={"Sec-Fetch-Site": "same-origin"},
    )
    assert resp.status_code == 500
    body = resp.json()
    assert body["reason"] == "unknown"


def test_unlock_returns_400_when_no_pass_entries(
    client_no_pass_entries: TestClient,
) -> None:
    resp = client_no_pass_entries.post(
        "/api/unlock",
        json={"passphrase": "x"},
        headers={"Sec-Fetch-Site": "same-origin"},
    )
    assert resp.status_code == 400
    assert resp.json()["reason"] == "no_pass_entries_configured"


# ── same-origin defense ───────────────────────────────────────────────


def test_unlock_blocks_cross_site(
    client_with_pass_entries: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def must_not_run(*_a: Any, **_k: Any) -> None:
        raise AssertionError("warm_gpg_agent should not have been called")

    monkeypatch.setattr("wishmap.secrets.warm_gpg_agent", must_not_run)
    resp = client_with_pass_entries.post(
        "/api/unlock",
        json={"passphrase": "x"},
        headers={"Sec-Fetch-Site": "cross-site"},
    )
    assert resp.status_code == 403


def test_unlock_blocks_cross_origin(
    client_with_pass_entries: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "wishmap.secrets.warm_gpg_agent",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("should not run")),
    )
    resp = client_with_pass_entries.post(
        "/api/unlock",
        json={"passphrase": "x"},
        headers={"Sec-Fetch-Site": "cross-origin"},
    )
    assert resp.status_code == 403


def test_unlock_allows_missing_sec_fetch_site_header(
    client_with_pass_entries: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Old browsers or curl without the header should not be blocked —
    same-origin defense is best-effort."""
    monkeypatch.setattr(
        "wishmap.secrets.warm_gpg_agent", lambda *_a, **_k: None
    )
    resp = client_with_pass_entries.post(
        "/api/unlock", json={"passphrase": "x"}
    )
    assert resp.status_code == 204


# ── log redaction ─────────────────────────────────────────────────────


def test_unlock_does_not_log_passphrase(
    client_with_pass_entries: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """CQ3 — the passphrase must never appear in log output, even on error."""
    sentinel = "DELIBERATELY_WRONG_xyzzy_capture_me"

    def fake_warm(entry: str, _p: str) -> None:
        # Logger output for the error path is what we're checking.
        # The exception message should never include the passphrase.
        raise BadPassphraseError(f"Wrong passphrase for pass entry '{entry}'")

    monkeypatch.setattr("wishmap.secrets.warm_gpg_agent", fake_warm)
    with caplog.at_level(logging.DEBUG):
        resp = client_with_pass_entries.post(
            "/api/unlock",
            json={"passphrase": sentinel},
            headers={"Sec-Fetch-Site": "same-origin"},
        )
    assert resp.status_code == 401
    # No log record should contain the sentinel.
    captured = "\n".join(
        rec.getMessage() for rec in caplog.records
    )
    assert sentinel not in captured
    # Response body must not contain it either.
    assert sentinel not in resp.text
