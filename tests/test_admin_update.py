"""Admins and the server self-update (`admins`, `updater`, /api/admin/update).

The update runs against a real git repository (a local "origin" cloned
into `<update_dir>/src`); only the build — `uv` and the network — is
replaced by a stub that makes an empty release, and the restart by a flag.
The launcher (`deploy/wishmap-launch`) runs for real on a fake release.
"""

import os
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import IO

import pytest
from fastapi.testclient import TestClient

import wishmap.app as app_module
from tests.helpers import PASSWORD, ctx, signed_in_client, user_id, user_state
from wishmap import admins, sync_runner
from wishmap.app import main
from wishmap.updater import RELEASE_MARKER, SHORT_SHA, Updater

LAUNCHER = Path(__file__).parent.parent / "deploy" / "wishmap-launch"


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
        },
    )
    return result.stdout.strip()


def _commit(repo: Path, message: str) -> str:
    (repo / "file.txt").write_text(message)
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "--quiet", "--no-gpg-sign", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    repo = tmp_path / "origin"
    repo.mkdir()
    _git(repo, "init", "--quiet", "--initial-branch=main")
    _commit(repo, "first")
    return repo


@pytest.fixture
def wishmap_toml() -> str:
    return 'title = "test"\nupdate_dir = "app"\n'


@pytest.fixture
def update_dir(install: Path, origin: Path) -> Path:
    root = install / "app"
    root.mkdir()
    _git(install, "clone", "--quiet", str(origin), str(root / "src"))
    return root


class Calls:
    def __init__(self) -> None:
        self.builds: list[str] = []
        self.restarts = 0


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> Calls:
    """Stub out the build (uv, network) and the restart (SIGTERM)."""
    calls = Calls()

    def build(self: Updater, sha: str, release: Path, log: IO[str]) -> None:
        calls.builds.append(sha)
        release.mkdir(parents=True)
        (release / RELEASE_MARKER).write_text(f"{sha}\n")

    def restart() -> None:
        calls.restarts += 1

    monkeypatch.setattr(Updater, "_build", build)
    monkeypatch.setattr(app_module, "_restart_soon", restart)
    return calls


@pytest.fixture
def admin_client(update_dir: Path, client: TestClient) -> Iterator[TestClient]:
    c = signed_in_client("alice")
    admins.set_admin(ctx().db_path, user_id("alice"), True)
    yield c
    c.close()


def _wait_settled(c: TestClient) -> dict[str, object]:
    for _ in range(200):
        status = c.get("/api/admin/update").json()
        if status["state"] != "updating":
            return status
        time.sleep(0.02)
    raise AssertionError("the update never finished")


# ── Admin rights ──────────────────────────────────────────────────────


def test_cli_grants_lists_and_revokes(
    user_client: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    main(["admin"])
    assert capsys.readouterr().out == "No admins.\n"
    main(["admin", "--user", "ALICE"])
    assert "alice is now an admin" in capsys.readouterr().out
    assert user_client.get("/api/me").json()["is_admin"] is True
    main(["admin"])
    assert capsys.readouterr().out == "alice\n"
    main(["admin", "--user", "alice", "--revoke"])
    assert user_client.get("/api/me").json()["is_admin"] is False


def test_cli_rejects_an_unknown_account(client: TestClient) -> None:
    with pytest.raises(SystemExit, match="No account named 'nobody'"):
        main(["admin", "--user", "nobody"])


def test_non_admins_are_refused(user_client: TestClient) -> None:
    assert user_client.get("/api/me").json()["is_admin"] is False
    assert user_client.get("/api/admin/update").status_code == 403
    resp = user_client.post("/api/admin/update", json={"password": PASSWORD})
    assert resp.status_code == 403
    assert resp.json() == {"detail": "admin_required"}
    assert user_client.post("/api/admin/update/check").status_code == 403


def test_an_api_token_is_refused_even_for_an_admin(admin_client: TestClient) -> None:
    created = admin_client.post("/api/tokens", json={"name": "t", "scope": "write"})
    token = created.json()["token"]
    with TestClient(app_module.app) as anon:
        resp = anon.get(
            "/api/admin/update", headers={"Authorization": f"Bearer {token}"}
        )
    assert resp.status_code == 403
    assert resp.json() == {"detail": "session_required"}


@pytest.mark.parametrize("wishmap_toml", ['title = "test"\n'])
def test_disabled_without_update_dir(user_client: TestClient) -> None:
    admins.set_admin(ctx().db_path, user_id("alice"), True)
    assert user_client.get("/api/admin/update").json()["enabled"] is False
    resp = user_client.post("/api/admin/update", json={"password": PASSWORD})
    assert resp.status_code == 404
    assert user_client.post("/api/admin/update/check").status_code == 404


# ── Check and update ──────────────────────────────────────────────────


def test_check_lists_the_new_commit(admin_client: TestClient, origin: Path) -> None:
    sha = _commit(origin, "Add a feature")
    status = admin_client.post("/api/admin/update/check").json()
    assert status["state"] == "idle", status
    assert status["latest"] == sha[:SHORT_SHA]
    # Not running from a release (a test venv): just the tip is listed.
    assert status["commits"] == [{"sha": sha[:SHORT_SHA], "subject": "Add a feature"}]


def test_update_switches_and_restarts(
    admin_client: TestClient, origin: Path, update_dir: Path, calls: Calls
) -> None:
    sha = _commit(origin, "second")
    resp = admin_client.post(
        "/api/admin/update", json={"password": PASSWORD, "commit": sha[:SHORT_SHA]}
    )
    assert resp.status_code == 202, resp.text
    status = _wait_settled(admin_client)
    assert status["state"] == "restarting", status
    assert calls.builds == [sha]
    assert calls.restarts == 1
    assert os.readlink(update_dir / "current") == f"releases/{sha[:SHORT_SHA]}"
    assert (update_dir / "pending").read_text() == "0\n"


def test_second_update_keeps_the_previous_release_and_prunes_older(
    admin_client: TestClient, origin: Path, update_dir: Path, calls: Calls
) -> None:
    shas = []
    for n in range(3):
        shas.append(_commit(origin, f"c{n}"))
        assert admin_client.post("/api/admin/update", json={"password": PASSWORD}).status_code == 202
        assert _wait_settled(admin_client)["state"] == "restarting"
        # As if the restart happened: the next process settles the update.
        ctx().updater._set(state="idle")  # type: ignore[union-attr]
    names = [s[:SHORT_SHA] for s in shas]
    assert os.readlink(update_dir / "current") == f"releases/{names[2]}"
    assert os.readlink(update_dir / "previous") == f"releases/{names[1]}"
    assert sorted(p.name for p in (update_dir / "releases").iterdir()) == sorted(names[1:])


def test_wrong_password_is_refused(admin_client: TestClient, calls: Calls) -> None:
    resp = admin_client.post("/api/admin/update", json={"password": "wrong password"})
    assert resp.status_code == 403
    assert resp.json() == {"detail": "wrong_password"}
    assert calls.builds == []


def test_a_moved_branch_deploys_nothing(
    admin_client: TestClient, origin: Path, update_dir: Path, calls: Calls
) -> None:
    shown = _commit(origin, "shown")
    _commit(origin, "pushed after the check")
    resp = admin_client.post(
        "/api/admin/update", json={"password": PASSWORD, "commit": shown[:SHORT_SHA]}
    )
    assert resp.status_code == 202
    status = _wait_settled(admin_client)
    assert status["state"] == "failed"
    assert "has moved on" in str(status["error"])
    assert calls.builds == [] and calls.restarts == 0
    assert not (update_dir / "current").exists()


def test_a_running_sync_blocks_the_update(admin_client: TestClient, calls: Calls) -> None:
    assert sync_runner.begin(user_state("alice"))
    resp = admin_client.post("/api/admin/update", json={"password": PASSWORD})
    assert resp.status_code == 409
    assert "sync" in resp.json()["detail"]
    assert calls.builds == []


def test_a_failing_step_reports_its_output(
    admin_client: TestClient, update_dir: Path, calls: Calls
) -> None:
    _git(update_dir / "src", "remote", "set-url", "origin", str(update_dir / "missing"))
    assert admin_client.post("/api/admin/update", json={"password": PASSWORD}).status_code == 202
    status = _wait_settled(admin_client)
    assert status["state"] == "failed"
    assert "git fetch failed" in str(status["error"])
    assert "missing" in str(status["error"])  # git's own message, from update.log
    assert calls.restarts == 0


def test_already_up_to_date(update_dir: Path, origin: Path, calls: Calls) -> None:
    sha = _git(origin, "rev-parse", "HEAD")
    updater = Updater(update_dir, "main", running=sha[:SHORT_SHA])
    assert updater.begin("updating")
    updater.run_update(None, lambda: None, lambda: None)
    assert updater.status().state == "idle"
    assert updater.status().message == "Up to date."
    assert calls.builds == []


def test_never_goes_back_to_an_older_commit(
    update_dir: Path, origin: Path, calls: Calls
) -> None:
    # Running a commit the branch hasn't caught up with (deployed before
    # it was pushed): its tip is older, so there is nothing to update.
    old = _git(origin, "rev-parse", "HEAD")
    ahead = _commit(origin, "deployed before it was pushed")
    _git(origin, "reset", "--quiet", "--hard", old)
    _git(update_dir / "src", "fetch", "--quiet", str(origin), ahead)
    updater = Updater(update_dir, "main", running=ahead[:SHORT_SHA])
    assert updater.begin("checking")
    updater.run_check()
    assert updater.status().commits == []
    assert "already contains" in str(updater.status().message)
    assert updater.begin("updating")
    updater.run_update(None, lambda: None, lambda: None)
    assert updater.status().state == "idle"
    assert calls.builds == []


def test_lists_the_commits_since_the_running_one(update_dir: Path, origin: Path) -> None:
    running = _git(origin, "rev-parse", "HEAD")
    _commit(origin, "one")
    _commit(origin, "two")
    updater = Updater(update_dir, "main", running=running[:SHORT_SHA])
    assert updater.begin("checking")
    updater.run_check()
    assert [c.subject for c in updater.status().commits] == ["two", "one"]
    assert updater.status().message is None


def test_warns_when_the_running_commit_is_not_in_the_history(
    update_dir: Path, origin: Path, calls: Calls
) -> None:
    tip = _commit(origin, "tip")
    updater = Updater(update_dir, "main", running="0123456789ab")
    assert updater.begin("checking")
    updater.run_check()
    assert [c.subject for c in updater.status().commits] == ["tip"]
    assert "not in main's history" in str(updater.status().message)
    # The admin saw the warning; updating still deploys the tip.
    assert updater.begin("updating")
    updater.run_update(tip[:SHORT_SHA], lambda: None, lambda: None)
    assert updater.status().state == "restarting"
    assert calls.builds == [tip]


# ── After the restart ────────────────────────────────────────────────


def test_open_settles_a_finished_update(tmp_path: Path) -> None:
    (tmp_path / "pending").write_text("1\n")
    status = Updater.open(tmp_path, "main").status()
    assert status.state == "idle" and status.message is not None
    assert not (tmp_path / "pending").exists()


def test_open_reports_a_rollback(tmp_path: Path) -> None:
    (tmp_path / "rolled-back").write_text("abc123\n")
    status = Updater.open(tmp_path, "main").status()
    assert status.state == "failed"
    assert "abc123" in str(status.error)
    assert not (tmp_path / "rolled-back").exists()


# ── The launcher ─────────────────────────────────────────────────────


def _fake_release(root: Path, name: str, *, works: bool) -> None:
    bin_dir = root / "releases" / name / "bin"
    bin_dir.mkdir(parents=True)
    uvicorn = bin_dir / "uvicorn"
    uvicorn.write_text(f'#!/bin/sh\necho "{name} $*"\nexit {0 if works else 1}\n')
    uvicorn.chmod(0o755)


def _launch(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(LAUNCHER), str(root), "--port", "1"], capture_output=True, text=True
    )


def test_launcher_starts_the_current_release(tmp_path: Path) -> None:
    _fake_release(tmp_path, "aaa", works=True)
    os.symlink("releases/aaa", tmp_path / "current")
    result = _launch(tmp_path)
    assert result.stdout == "aaa wishmap.app:app --port 1\n"


def test_launcher_rolls_back_a_release_that_never_starts(tmp_path: Path) -> None:
    _fake_release(tmp_path, "old", works=True)
    _fake_release(tmp_path, "new", works=False)
    os.symlink("releases/new", tmp_path / "current")
    os.symlink("releases/old", tmp_path / "previous")
    (tmp_path / "pending").write_text("0\n")
    for tries in (1, 2, 3):
        assert _launch(tmp_path).stdout.startswith("new ")
        assert (tmp_path / "pending").read_text() == f"{tries}\n"
    result = _launch(tmp_path)
    assert result.stdout.startswith("old ")
    assert os.readlink(tmp_path / "current") == "releases/old"
    assert (tmp_path / "rolled-back").read_text() == "new\n"
    assert not (tmp_path / "pending").exists()


def test_cli_update_builds_and_switches_without_restarting(
    update_dir: Path, origin: Path, calls: Calls, capsys: pytest.CaptureFixture[str]
) -> None:
    sha = _commit(origin, "from the shell")
    main(["update"])
    assert f"is now {sha[:SHORT_SHA]}: restart" in capsys.readouterr().out
    assert os.readlink(update_dir / "current") == f"releases/{sha[:SHORT_SHA]}"
    assert calls.restarts == 0


@pytest.mark.parametrize("wishmap_toml", ['title = "test"\n'])
def test_cli_update_needs_update_dir(install: Path) -> None:
    with pytest.raises(SystemExit, match="update_dir is not set"):
        main(["update"])
