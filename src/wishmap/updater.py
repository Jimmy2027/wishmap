"""Self-update: deploy the newest commit of a branch, then restart (admins only).

Off unless `update_dir` is set in `wishmap.toml`. That directory belongs to
the server's own user, which does everything here without root:

    <update_dir>/
      src/                      a git clone; updates come from its `origin`
      releases/<sha>/           one virtualenv per deployed commit
      current -> releases/<sha> the release the launcher starts
      previous -> releases/<sha>  the one before it (the rollback target)
      cache/                    uv's cache
      gnupg/                    the keys `git verify-commit` trusts
      update.log                the output of the last update
      pending                   start attempts of a new release (launcher)
      rolled-back               a release the launcher gave up on

An update fetches the branch, builds its newest commit into a venv of its
own (as `install.sh` does: `uv export` + `uv build` + `uv pip install`),
points `current` at it and ends the server process. The service supervisor
(OpenRC's supervise-daemon) then starts it again through the launcher
(`deploy/wishmap-launch`), now on the new release. The launcher counts
start attempts in `pending`, which `Updater.open` removes once a release
got as far as starting up; after a few failed attempts it points `current`
back at `previous` and leaves `rolled-back`.

The steps run in a worker thread (they take minutes); the status is kept
in memory, so a restart starts afresh — except for what `pending` and
`rolled-back` tell `open` about the update that caused it.
"""

import logging
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import IO

from wishmap.models import UpdateCommit, UpdateState, UpdateStatus

logger = logging.getLogger(__name__)

# Release directories (and what the UI shows) use this many hex digits.
SHORT_SHA = 12
# Written last into a release: a directory without it is a half-built one.
RELEASE_MARKER = "wishmap-release"
# Any single git/uv step taking longer than this is given up on.
STEP_TIMEOUT_SEC = 15 * 60
# The most commits a check lists between the running release and the tip.
MAX_LISTED_COMMITS = 50
# Lines of `update.log` quoted in a failed update's error.
LOG_TAIL_LINES = 15

# States in which an update or check is in flight (`begin` refuses another).
_BUSY_STATES: frozenset[UpdateState] = frozenset({"checking", "updating", "restarting"})


class UpdateError(Exception):
    """An update step failed; the message is meant for the admin."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _short(sha: str) -> str:
    return sha[:SHORT_SHA]


def _symlink(link: Path, target: str) -> None:
    """Point `link` at `target`, atomically replacing what it pointed at."""
    tmp = link.with_name(f".{link.name}.tmp")
    tmp.unlink(missing_ok=True)
    os.symlink(target, tmp)
    os.replace(tmp, link)


class Updater:
    """The self-update of one server process (see the module docstring)."""

    def __init__(
        self,
        root: Path,
        branch: str,
        *,
        require_signed: bool = False,
        running: str | None = None,
    ) -> None:
        self.root = root
        self.branch = branch
        self.require_signed = require_signed
        # The release this process runs (a directory name under releases/),
        # or None when it doesn't run from one (a dev checkout).
        self.running = running
        self._lock = threading.Lock()
        self._status = UpdateStatus(enabled=True, branch=branch, running=running)

    @classmethod
    def open(cls, root: Path, branch: str, *, require_signed: bool = False) -> "Updater":
        """Set up the updater at server start, settling a finished update.

        `pending` still there means this process is the release an update
        switched to, and it got this far: the update is done. `rolled-back`
        means the launcher gave up on a release and started the previous
        one: the update failed.
        """
        running = running_release(root)
        updater = cls(root, branch, require_signed=require_signed, running=running)
        if updater.rolled_back_path.exists():
            failed = updater.rolled_back_path.read_text().strip()
            updater.rolled_back_path.unlink()
            updater.pending_path.unlink(missing_ok=True)
            logger.warning("Release %s did not start; rolled back to %s", failed, running)
            updater._set(
                state="failed",
                error=f"Release {failed} did not start up; rolled back to {running}. "
                "See the server log.",
                finished_at=_now_iso(),
            )
        elif updater.pending_path.exists():
            updater.pending_path.unlink()
            logger.info("Updated to release %s", running)
            updater._set(message=f"Updated to {running}.", finished_at=_now_iso())
        return updater

    # ── Paths ──

    @property
    def src(self) -> Path:
        return self.root / "src"

    @property
    def releases(self) -> Path:
        return self.root / "releases"

    @property
    def current(self) -> Path:
        return self.root / "current"

    @property
    def previous(self) -> Path:
        return self.root / "previous"

    @property
    def pending_path(self) -> Path:
        return self.root / "pending"

    @property
    def rolled_back_path(self) -> Path:
        return self.root / "rolled-back"

    @property
    def log_path(self) -> Path:
        return self.root / "update.log"

    # ── Status ──

    def status(self) -> UpdateStatus:
        with self._lock:
            return self._status

    def _set(self, **changes: object) -> None:
        with self._lock:
            self._status = self._status.model_copy(update=changes)

    def begin(self, state: UpdateState) -> bool:
        """Flip to `state` ("checking" or "updating") unless one is in flight.

        Synchronous, like `sync_runner.begin`, so two requests can't both
        start one. The caller then runs `run_check` / `run_update`.
        """
        with self._lock:
            if self._status.state in _BUSY_STATES:
                return False
            self._status = self._status.model_copy(
                update={"state": state, "step": None, "message": None, "error": None}
            )
            return True

    def _fail(self, error: str) -> None:
        self._set(state="failed", step=None, error=error, finished_at=_now_iso())

    # ── Check ──

    def run_check(self) -> None:
        """Fetch the branch and list what an update would deploy (after `begin`)."""
        try:
            with open(self.log_path, "w") as log:
                self._set(step=f"Fetching {self.branch}")
                sha = self._fetch(log)
                commits, message = self._compare_with_running(sha)
        except UpdateError as e:
            self._fail(str(e))
            return
        except Exception as e:
            logger.exception("Checking for an update failed")
            self._fail(f"Checking for an update failed: {e}")
            return
        self._set(
            state="idle",
            step=None,
            latest=_short(sha),
            commits=commits,
            checked_at=_now_iso(),
            message=message,
        )

    def _is_ancestor(self, older: str, newer: str) -> bool:
        """Whether commit `older` is in the history of `newer` (False when
        git doesn't know one of them)."""
        return self._git_output("merge-base", "--is-ancestor", older, newer, check=False) is not None

    def _up_to_date(self, sha: str) -> str | None:
        """Why the running release needs no update to `sha`, or None.

        Also when the running commit is *ahead* of the branch (deployed
        from a commit not pushed yet): deploying `sha` would go back.
        """
        if self.running is None:
            return None
        if self.running == _short(sha):
            return "Up to date."
        if self._is_ancestor(sha, self.running):
            return (
                f"Running {self.running}, which already contains {self.branch}'s "
                f"newest commit {_short(sha)}."
            )
        return None

    def _compare_with_running(self, sha: str) -> tuple[list[UpdateCommit], str | None]:
        """The commits an update to `sha` brings (newest first), and a note.

        Just `sha` itself, with a warning, when the running commit isn't in
        the branch's history (then git can't tell what an update adds, nor
        whether it goes back).
        """
        if (reason := self._up_to_date(sha)) is not None:
            return [], reason
        message = None
        if self.running is not None and self._is_ancestor(self.running, sha):
            rev = [f"{self.running}..{sha}"]
        else:
            rev = ["-n1", sha]
            if self.running is not None:
                message = (
                    f"The running commit {self.running} is not in {self.branch}'s "
                    f"history: an update deploys {_short(sha)} even if it is older."
                )
        out = self._git_output("log", f"-n{MAX_LISTED_COMMITS}", "--format=%H%x09%s", *rev) or ""
        commits = []
        for line in out.splitlines():
            full, _, subject = line.partition("\t")
            commits.append(UpdateCommit(sha=_short(full), subject=subject))
        return commits, message

    # ── Update ──

    def run_update(
        self,
        expected: str | None,
        busy: Callable[[], str | None],
        restart: Callable[[], None],
    ) -> None:
        """Deploy the branch's newest commit and restart (after `begin`).

        `expected`: the (short) commit the admin was shown; if the branch
        has moved on since, nothing is deployed. `busy` names a reason not
        to restart now (a running sync), checked once the build is done.
        `restart` ends the process, so the supervisor starts the new
        release.
        """
        try:
            with open(self.log_path, "w") as log:
                self._set(step=f"Fetching {self.branch}")
                sha = self._fetch(log)
                short = _short(sha)
                if expected is not None and not sha.startswith(expected):
                    raise UpdateError(
                        f"{self.branch} has moved on to {short} since the check; "
                        "check again."
                    )
                if (reason := self._up_to_date(sha)) is not None:
                    self._set(
                        state="idle", step=None, latest=short, commits=[],
                        message=reason, finished_at=_now_iso(),
                    )
                    return
                if self.require_signed:
                    self._set(step=f"Verifying the signature of {short}")
                    self._run(log, ["git", "-C", str(self.src), "verify-commit", sha])
                release = self.releases / short
                if not (release / RELEASE_MARKER).is_file():
                    self._set(step=f"Building {short}")
                    self._build(sha, release, log)
                reason = busy()
                if reason is not None:
                    raise UpdateError(f"{reason} The new release is built: try again then.")
                self._set(step=f"Switching to {short}")
                self._switch(release)
                self._prune()
        except UpdateError as e:
            self._fail(str(e) + self._log_tail())
            return
        except Exception as e:
            logger.exception("Update failed")
            self._fail(f"Update failed: {e}" + self._log_tail())
            return
        logger.info("Switched to release %s", short)
        self._set(state="restarting", step=None, latest=short)
        restart()

    def _fetch(self, log: IO[str]) -> str:
        """Fetch the branch into the clone; return its newest commit's sha."""
        if not (self.src / ".git").exists():
            raise UpdateError(f"No git clone at {self.src}: see README, Deployment.")
        ref = f"refs/remotes/origin/{self.branch}"
        self._run(
            log,
            ["git", "-C", str(self.src), "fetch", "--quiet", "--no-tags", "origin",
             f"+refs/heads/{self.branch}:{ref}"],
        )
        sha = self._git_output("rev-parse", "--verify", f"{ref}^{{commit}}")
        if not sha:
            raise UpdateError(f"origin has no branch {self.branch!r}")
        return sha.strip()

    def _build(self, sha: str, release: Path, log: IO[str]) -> None:
        """Build commit `sha` into a fresh venv at `release` (marked complete last)."""
        uv = shutil.which("uv")
        if uv is None:
            raise UpdateError("uv is not on the server's PATH")
        if release.exists():
            shutil.rmtree(release)  # a half-built one
        self.releases.mkdir(exist_ok=True)
        python = release / "bin" / "python"
        with tempfile.TemporaryDirectory(dir=self.root, prefix=".build-") as tmp_name:
            tmp = Path(tmp_name)
            src = tmp / "src"
            archive = tmp / "src.tar"
            with open(archive, "wb") as out:
                self._run(log, ["git", "-C", str(self.src), "archive", "--format=tar", sha], stdout=out)
            with tarfile.open(archive) as tar:
                tar.extractall(src, filter="data")
            requirements = tmp / "requirements.txt"
            self._run(
                log,
                [uv, "export", "--frozen", "--no-dev", "--no-emit-project", "--no-hashes",
                 "--quiet", "-o", str(requirements)],
                cwd=src,
            )
            self._run(log, [uv, "build", "--wheel", "-o", str(tmp / "dist")], cwd=src)
            wheels = list((tmp / "dist").glob("*.whl"))
            if len(wheels) != 1:
                raise UpdateError(f"uv build made {len(wheels)} wheels, expected one")
            try:
                version = f"{sys.version_info.major}.{sys.version_info.minor}"
                self._run(log, [uv, "venv", "--python", version, str(release)])
                self._run(log, [uv, "pip", "install", "--python", str(python), "-r", str(requirements)])
                self._run(
                    log, [uv, "pip", "install", "--python", str(python), "--no-deps", str(wheels[0])]
                )
                # Catches a release that can't even be imported before it is
                # switched to (the launcher would roll it back, but slower).
                self._run(log, [str(python), "-c", "import wishmap.app"])
                (release / RELEASE_MARKER).write_text(f"{sha}\n")
            except BaseException:
                shutil.rmtree(release, ignore_errors=True)
                raise

    def _switch(self, release: Path) -> None:
        """Make `release` current (the old current becomes previous) and arm
        the launcher's rollback."""
        target = f"releases/{release.name}"
        if self.current.is_symlink():
            old = os.readlink(self.current)
            if old != target:
                _symlink(self.previous, old)
        _symlink(self.current, target)
        self.pending_path.write_text("0\n")

    def _prune(self) -> None:
        """Delete every release but current and previous."""
        keep = {
            Path(os.readlink(link)).name
            for link in (self.current, self.previous)
            if link.is_symlink()
        }
        for path in self.releases.iterdir():
            if path.is_dir() and not path.is_symlink() and path.name not in keep:
                shutil.rmtree(path, ignore_errors=True)

    # ── Subprocesses ──

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        # The server's own venv must not become uv's target.
        env.pop("VIRTUAL_ENV", None)
        env.update(
            # The service user's home may not be writable (or readable).
            HOME=str(self.root),
            UV_CACHE_DIR=str(self.root / "cache"),
            UV_PYTHON_DOWNLOADS="never",
            GIT_TERMINAL_PROMPT="0",
            GNUPGHOME=str(self.root / "gnupg"),
        )
        return env

    def _run(
        self,
        log: IO[str],
        args: list[str],
        *,
        cwd: Path | None = None,
        stdout: IO[bytes] | None = None,
    ) -> None:
        """Run one step, its output going to `log` (or stdout to `stdout`)."""
        # "git fetch", "uv pip", …: the program and its (sub)command.
        words = [Path(args[0]).name, *(a for a in args[1:] if not a.startswith("-"))]
        if words[0] == "git":
            del words[1]  # the clone, after -C
        what = " ".join(words[:2])
        log.write(f"$ {' '.join(args)}\n")
        log.flush()
        try:
            result = subprocess.run(
                args,
                cwd=cwd,
                env=self._env(),
                stdin=subprocess.DEVNULL,
                stdout=stdout if stdout is not None else log,
                stderr=log,
                timeout=STEP_TIMEOUT_SEC,
            )
        except subprocess.TimeoutExpired:
            raise UpdateError(f"{what} took too long") from None
        except OSError as e:
            raise UpdateError(f"Could not run {args[0]}: {e}") from None
        if result.returncode != 0:
            raise UpdateError(f"{what} failed (exit {result.returncode})")

    def _git_output(self, *args: str, check: bool = True) -> str | None:
        """The stdout of a git command in the clone; None if it failed and
        `check` is off."""
        result = subprocess.run(
            ["git", "-C", str(self.src), *args],
            env=self._env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=STEP_TIMEOUT_SEC,
        )
        if result.returncode != 0:
            if not check:
                return None
            raise UpdateError(f"git {args[0]} failed: {result.stderr.strip()}")
        return result.stdout

    def _log_tail(self) -> str:
        try:
            lines = self.log_path.read_text(errors="replace").splitlines()
        except OSError:
            return ""
        tail = "\n".join(lines[-LOG_TAIL_LINES:])
        return f"\n\n{tail}" if tail else ""


def running_release(root: Path) -> str | None:
    """The release (directory name under `<root>/releases`) this process
    runs from, or None when it doesn't run from one."""
    prefix = Path(sys.prefix).resolve()
    try:
        releases = (root / "releases").resolve()
    except OSError:
        return None
    return prefix.name if prefix.parent == releases else None
