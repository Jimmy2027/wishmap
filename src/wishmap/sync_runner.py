"""Background runner that syncs one user's Garmin account and refreshes their state.

Every function takes the user's `UserState`: the status dict, the run lock
and the pending-MFA slot all live there, so two users' syncs never share
(or block on) anything. The Garmin work itself runs on a
`garmin.GarminSyncContext` the HTTP handler builds for the user (their
settings, unsealed tokens, and the password if they just entered it).

State machine
─────────────

                  POST /api/sync
                       │
                       ▼
              begin(state) ─── False ──→ 409 Conflict (current status)
                       │
                      True
                       ▼
              create_task(run_sync(state, garmin_ctx))
              return 202 (state="running")
                       │
                       ▼
          garmin_ctx is None (no Garmin account set)
            → services.garmin = {state:"needs_garmin_login", …}
          else try: garmin.sync(garmin_ctx)
          except GarminLoginRequiredError as e:
            services.garmin={state:"needs_garmin_login", error:str(e)}
            top-level needs_garmin_login = True
          except MfaRequiredError as e:
            services.garmin={state:"needs_mfa", error:str(e)}
            top-level needs_mfa = True
          except SyncError as e:
            services.garmin={state:"error", error:str(e)}
          else:
            services.garmin={state:"done", error:None}
                       │
                       ▼
              state.invalidate()   (only if garmin is "done")
              top-level state = "done" if garmin is done, else "error"
                       │
                       ▼
              finished_at = now

Top-level state stays in {idle, running, done, error}. `needs_mfa` and
`needs_garmin_login` are separate signals — when one is True, the web UI
shows the MFA-code or Garmin-login modal instead of an error tooltip,
regardless of top-level state (`error`, since Garmin didn't reach "done").

Status dict shape (returned by get_status, JSON-serialized to the UI):

    {
      "state": "idle" | "running" | "done" | "error",
      "started_at": ISO-8601 or null,
      "finished_at": ISO-8601 or null,
      "services": {
        "garmin": {"state": "idle|running|done|error|needs_mfa|"
                            "needs_garmin_login",
                   "error": str|null}
      },
      "error": str|null,          # run-level error (e.g. the run task was
                                  # cancelled); Garmin's own error lives
                                  # under "services"
      "needs_mfa": bool,          # Garmin login waiting for an MFA code
      "needs_garmin_login": bool  # Garmin needs the user's password
    }

The Garmin password never enters the status (nor any log line): it lives
only in the `GarminSyncContext` of the one run it was posted with.

Refresh: a successful sync only *invalidates* the user's built map data;
the next GET rebuilds it (userdata.ensure_loaded). So the run's terminal
state is reported as soon as Garmin finishes, and the rebuild's cost lands
on the frontend's post-sync refetch instead.

uvicorn dev-server reload note: sync writes GPX files under
data/users/<id>/gpx/garmin/. uvicorn's default watchfiles reloader only
watches .py files, so these writes do not retrigger a reload. If a future
change adds --reload-dirs covering data/, this module will need to opt
those paths out (or the server will restart mid-sync).
"""

import asyncio
import logging
from typing import Any

from wishmap import garmin
from wishmap.exceptions import GarminLoginRequiredError, MfaRequiredError, SyncError
from wishmap.garmin import GarminSyncContext
from wishmap.state import UserState, now_iso

logger = logging.getLogger("wishmap.sync_runner")

# services.garmin.error when the user has no Garmin account set at all.
NO_GARMIN_ACCOUNT = "Enter your Garmin login to sync."


def get_status(state: UserState) -> dict[str, Any]:
    """Return a shallow snapshot of the user's current sync status."""
    status = state.sync_status
    return {
        "state": status["state"],
        "started_at": status["started_at"],
        "finished_at": status["finished_at"],
        "services": {"garmin": dict(status["services"]["garmin"])},
        "error": status["error"],
        "needs_mfa": status["needs_mfa"],
        "needs_garmin_login": status["needs_garmin_login"],
    }


def is_running(state: UserState) -> bool:
    """True if a sync is currently in flight for this user."""
    return state.sync_status["state"] == "running"


def begin(state: UserState) -> bool:
    """Synchronously flip the user's state to running.

    Called by the POST /api/sync handler before scheduling run_sync. Returns
    False if a sync is already in flight (caller returns 409); True if the
    flip succeeded (caller schedules run_sync and returns 202).

    Doing the flip synchronously here closes a race where two POSTs arriving
    in the same event-loop tick could both pass the is_running() check before
    the first task got to acquire the lock.
    """
    status = state.sync_status
    if status["state"] == "running":
        return False
    status["state"] = "running"
    status["started_at"] = now_iso()
    status["finished_at"] = None
    status["error"] = None
    status["needs_mfa"] = False
    status["needs_garmin_login"] = False
    status["services"]["garmin"] = {"state": "idle", "error": None}
    return True


async def run_sync(state: UserState, garmin_ctx: GarminSyncContext | None) -> None:
    """Sync the user's Garmin account, then invalidate their map data.

    `garmin_ctx` is None when the user has no Garmin account set; the run
    then ends asking for a Garmin login.

    Assumes begin(state) was called synchronously by the caller before
    scheduling this coroutine. If invoked directly without a prior begin(),
    the user's sync_lock serves as a backstop against concurrent runs.
    """
    status = state.sync_status
    services = status["services"]
    async with state.sync_lock:
        try:
            if garmin_ctx is None:
                services["garmin"] = {
                    "state": "needs_garmin_login",
                    "error": NO_GARMIN_ACCOUNT,
                }
                status["needs_garmin_login"] = True
            else:
                services["garmin"]["state"] = "running"
                try:
                    await asyncio.to_thread(garmin.sync, garmin_ctx)
                except GarminLoginRequiredError as e:
                    logger.info(
                        "Garmin sync of user %d needs a login: %s", state.user_id, e
                    )
                    services["garmin"] = {"state": "needs_garmin_login", "error": str(e)}
                    status["needs_garmin_login"] = True
                except MfaRequiredError as e:
                    logger.info(
                        "Garmin sync of user %d needs an MFA code", state.user_id
                    )
                    services["garmin"] = {"state": "needs_mfa", "error": str(e)}
                    status["needs_mfa"] = True
                except SyncError as e:
                    logger.warning("Garmin sync of user %d failed: %s", state.user_id, e)
                    services["garmin"] = {"state": "error", "error": str(e)}
                except Exception as e:
                    logger.exception("Garmin sync of user %d crashed", state.user_id)
                    services["garmin"] = {
                        "state": "error",
                        "error": f"unexpected error: {e}",
                    }
                else:
                    services["garmin"] = {"state": "done", "error": None}
                finally:
                    # The password was for this one run only.
                    garmin_ctx.password = None

            # Drop the user's built map data so the next GET rebuilds it
            # with the new routes and GPX files. Skipped when nothing synced:
            # the rebuild re-parses every GPX file, so keep the warm cache
            # when nothing changed. Off the event loop: invalidate() waits
            # out an in-flight build.
            if services["garmin"]["state"] == "done":
                await asyncio.to_thread(state.invalidate)

            if services["garmin"]["state"] == "done" and status["error"] is None:
                status["state"] = "done"
            else:
                status["state"] = "error"
        finally:
            status["finished_at"] = now_iso()
            # An unexpected crash above (e.g. a cancelled task) must not leave
            # the user stuck in "running": begin() would 409 forever.
            if status["state"] == "running":
                status["state"] = "error"
                status["error"] = "sync interrupted"
