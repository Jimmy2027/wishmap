"""Background runner that syncs configured services and refreshes app state.

State machine
─────────────

                  POST /api/sync
                       │
                       ▼
                  begin() ─── False ──→ 409 Conflict (current status)
                       │
                      True
                       ▼
              create_task(run_sync())
              return 202 (state="running")
                       │
                       ▼
        for svc in [strava, garmin]:
          try: svc.sync(...)
          except SyncError as e:
            services[svc]={state:"error", error:str(e)}
          else:
            services[svc]={state:"done", error:None}
                       │
                       ▼
              app.reload_data()
              ├── ok  → top-level state="done"
              └── err → top-level state="error"
                       │
                       ▼
              finished_at = now

Status dict shape (returned by get_status, JSON-serialized to the UI):

    {
      "state": "idle" | "running" | "done" | "error",
      "started_at": ISO-8601 or null,
      "finished_at": ISO-8601 or null,
      "services": {
        "strava": {"state": "idle|skipped|running|done|error", "error": str|null},
        "garmin": {"state": "idle|skipped|running|done|error", "error": str|null}
      },
      "error": str|null  # top-level error (e.g., reload_data failure)
    }

uvicorn dev-server reload note: sync writes to data/garmin/*.gpx,
data/strava/activities.db, and data/garmin/garmin.toml. uvicorn's default
watchfiles reloader only watches .py files, so these writes do not retrigger
a reload. If a future change adds --reload-dirs covering data/, this module
will need to opt those paths out (or the server will restart mid-sync).
"""

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from wishmap import garmin, strava
from wishmap.exceptions import SyncError
from wishmap.models import WishmapConfig

logger = logging.getLogger("wishmap.sync_runner")

# Defense-in-depth — begin() flipping `_status["state"]` is already the
# primary gate against concurrent runs (synchronous flip from a coroutine
# is atomic). The lock catches the unlikely case where two coroutines call
# run_sync() directly without going through begin().
_lock = asyncio.Lock()


def _initial_status() -> dict[str, Any]:
    return {
        "state": "idle",
        "started_at": None,
        "finished_at": None,
        "services": {
            "strava": {"state": "idle", "error": None},
            "garmin": {"state": "idle", "error": None},
        },
        "error": None,
    }


_status: dict[str, Any] = _initial_status()


def get_status() -> dict[str, Any]:
    """Return a shallow snapshot of the current sync status."""
    return {
        "state": _status["state"],
        "started_at": _status["started_at"],
        "finished_at": _status["finished_at"],
        "services": {
            "strava": dict(_status["services"]["strava"]),
            "garmin": dict(_status["services"]["garmin"]),
        },
        "error": _status["error"],
    }


def is_running() -> bool:
    """True if a sync is currently in flight."""
    return _status["state"] == "running"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def begin() -> bool:
    """Synchronously flip state to running.

    Called by the POST /api/sync handler before scheduling run_sync. Returns
    False if a sync is already in flight (caller returns 409); True if the
    flip succeeded (caller schedules run_sync and returns 202).

    Doing the flip synchronously here closes a race where two POSTs arriving
    in the same event-loop tick could both pass the is_running() check before
    the first task got to acquire the lock.
    """
    if _status["state"] == "running":
        return False
    _status["state"] = "running"
    _status["started_at"] = _now_iso()
    _status["finished_at"] = None
    _status["error"] = None
    for svc in ("strava", "garmin"):
        _status["services"][svc] = {"state": "idle", "error": None}
    return True


async def run_sync(config: WishmapConfig, base_path: Path) -> None:
    """Sync configured services in order (strava → garmin), then reload app data.

    Order matches the CLI `--sync` flow: Strava first so its activities DB
    exists when Garmin sync looks up matches via strava.find_match.

    A per-service error is captured but does not abort the whole run — a
    Strava token expiry does not block Garmin from syncing.

    Assumes begin() was called synchronously by the caller before scheduling
    this coroutine. If invoked directly without a prior begin(), the lock
    serves as a backstop against concurrent runs.
    """
    async with _lock:
        try:
            if config.strava is None:
                _status["services"]["strava"] = {"state": "skipped", "error": None}
            else:
                _status["services"]["strava"]["state"] = "running"
                try:
                    await asyncio.to_thread(strava.sync, config.strava, base_path)
                except SyncError as e:
                    logger.warning("Strava sync failed: %s", e)
                    _status["services"]["strava"] = {
                        "state": "error",
                        "error": str(e),
                    }
                except Exception as e:
                    logger.exception("Strava sync crashed")
                    _status["services"]["strava"] = {
                        "state": "error",
                        "error": f"unexpected error: {e}",
                    }
                else:
                    _status["services"]["strava"] = {"state": "done", "error": None}

            if config.garmin is None:
                _status["services"]["garmin"] = {"state": "skipped", "error": None}
            else:
                _status["services"]["garmin"]["state"] = "running"
                strava_db: Path | None = None
                if config.strava is not None:
                    strava_db = base_path / config.strava.gpx_dir / "activities.db"
                try:
                    await asyncio.to_thread(
                        garmin.sync, config.garmin, base_path, strava_db
                    )
                except SyncError as e:
                    logger.warning("Garmin sync failed: %s", e)
                    _status["services"]["garmin"] = {
                        "state": "error",
                        "error": str(e),
                    }
                except Exception as e:
                    logger.exception("Garmin sync crashed")
                    _status["services"]["garmin"] = {
                        "state": "error",
                        "error": f"unexpected error: {e}",
                    }
                else:
                    _status["services"]["garmin"] = {"state": "done", "error": None}

            # Reload in-memory data so the running server picks up new GPX
            # files and garmin.toml entries without a restart. Done even
            # if one service errored — the other may have produced new
            # data worth surfacing.
            try:
                # Lazy import to avoid a circular dependency with app.py,
                # which imports sync_runner to wire up endpoints.
                from wishmap import app as app_module

                await asyncio.to_thread(app_module.reload_data)
            except SyncError as e:
                logger.warning("reload_data failed: %s", e)
                _status["error"] = str(e)
            except Exception as e:
                logger.exception("reload_data crashed")
                _status["error"] = f"reload failed: {e}"

            # Top-level state: "done" if every non-skipped service succeeded
            # AND reload was clean; "error" otherwise.
            services_ok = all(
                svc["state"] in ("done", "skipped")
                for svc in _status["services"].values()
            )
            if services_ok and _status["error"] is None:
                _status["state"] = "done"
            else:
                _status["state"] = "error"
        finally:
            _status["finished_at"] = _now_iso()


def reset_status_for_tests() -> None:
    """Test-only helper to reset module state between test cases."""
    global _status
    _status = _initial_status()
