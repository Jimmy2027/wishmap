# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
uv sync                          # install dependencies
uv run wishmap                   # dev server (= `wishmap serve`, auto-reload) at http://127.0.0.1:8000
uv run wishmap --host 0.0.0.0 --port 3000  # custom host/port
uv run wishmap --config /path/to/wishmap.toml  # custom config (or WISHMAP_CONFIG)
uv run wishmap import-toml --user NAME wishmap.toml  # move a single-user install into an account
uv run wishmap export --user NAME ./public           # static read-only snapshot of an account's map
uv run pytest                    # tests (also a pre-commit hook)
uv run pyright src/ tests/       # type-check (src/ is a pre-commit hook; keep tests/ clean too)
```

Never point a dev server, an import or an export at the real `data/` directory for experiments — work on a copy.

## Architecture

Wishmap is a self-hosted, multi-user map app for outdoor sports (hiking, biking, ski touring). Each account has its own pins, GPX routes and ratings on a Leaflet/OpenStreetMap basemap, can sync Garmin Connect activities (named from an uploaded Strava archive), and can publish a read-only public link. `wishmap.toml` holds only server settings; all per-user data is in SQLite plus per-user GPX files. See README.md for the user-facing behaviour, security model and deployment.

**Backend** (FastAPI + uvicorn, Python 3.13), `src/wishmap/`:
- `app.py` — the FastAPI app: `lifespan` (loads settings, migrates the DB, puts an `AppContext` on `app.state.ctx`), every `/api/*` route (pins/routes CRUD, ratings, integrations, sync, MFA, public link), the public `/u/{token}` view and its `/u/{token}/api/*`, routes shared with other accounts (`/api/sharing`, `/api/shared-users`, `/api/shared/{user_id}/routes`: notes and `strava_id` stripped, never in the read-only views), and the CLI (`main()`: `serve` / `import-toml` / `export` subcommands).
- `auth.py` — cookies (`wishmap_session` Lax, `wishmap_vault` Strict, both HttpOnly), `/api/auth/{register,login,logout,password}`, and the dependencies `Ctx`, `CurrentUser` (401), `VaultKey` (409 `vault_locked`), `SameOrigin` (CSRF 403; required on **every** mutating endpoint).
- `users.py` — accounts (scrypt password hashes) and sessions (sha256 of the token stored); unwraps/re-wraps the vault key on login/password change.
- `vault.py` — per-user AES-GCM vault: key wrapped under a scrypt KEK of the password; `seal`/`unseal` provider secrets (Garmin tokens), bound to user + provider.
- `state.py` — `AppContext` (registry, settings, paths, `LoginThrottle`), `StateRegistry`, and `UserState` (cached GeoJSON, sync status/lock, `PendingMfaSlot`). No module globals.
- `userdata.py` — `build()` (DB rows -> pins/routes FeatureCollections; shared with the export) and `ensure_loaded()`, the lazy per-user cache (first read after start/invalidate parses every GPX).
- `schema.py` — the whole SQLite schema plus the idempotent `migrate()` of a legacy single-user DB (old rows go to unclaimed `user_id = 0`). `db.py` — `connect()` (WAL, foreign keys, migrate on every open).
- Store modules, one per table, all keyed on `user_id`: `routes_store.py` (source `web`/`garmin`/`import`, `replace_source` for Garmin sync), `pins_store.py`, `ratings.py`, `integrations_store.py` (settings JSON + sealed tokens), `public_links.py`, `shared_maps.py` (opt-in to share one's routes with every other account), `track_cache.py` (each GPX's thinned geometry, stamped with mtime/size, so a restart doesn't re-parse every file).
- `garmin.py` — `sync(GarminSyncContext)`: login from unsealed tokens, else the password given with this sync, else `GarminLoginRequiredError`; MFA parked on the user's `PendingMfaSlot` (`complete_mfa`); downloads GPX to `<user dir>/gpx/garmin/` and replaces the user's Garmin routes. New tokens go out via `on_tokens` (the caller seals them).
- `strava.py` — Strava archive (`.zip`/`activities.csv`) import into `<user dir>/strava/activities.db` and `find_match` for Garmin activities. No Strava API/OAuth.
- `ridewithgps.py` — import of a public Ride with GPS trip/route from its link (`POST /api/routes/import`): parses the link to a digits-only id, fetches the undocumented `ridewithgps.com/<trips|routes>/<id>.json` (fixed host, same-site redirects only, size-capped), writes it as GPX under `gpx/web/`. No API key/OAuth, so private items are refused.
- `sync_runner.py` — background sync state machine per `UserState`; invalidates the user's cached map after a successful sync.
- `config.py` — `load_settings()` (`ServerSettings`; legacy single-user keys get one warning) and `load_user_toml()` (legacy TOML with `includes`, the importer's input).
- `importer.py` — `import_toml()`: legacy TOML + GPX + unclaimed DB rows + Strava DB + `[garmin]` settings into an account (idempotent).
- `export.py` — `export_site()` static snapshot, and `transform_html()` which bakes the read-only view (also used by `/u/{token}`).
- `models.py` — Pydantic models (settings, `PinConfig`/`RouteConfig`, GeoJSON, request/response bodies). `gpx.py` parses GPX to `[lon, lat]` and thins it to display geometry (`track_parts`: split at gaps, Douglas-Peucker, rounding); `geojson.py` builds features (incl. derived route-start points); `exceptions.py` holds `SyncError` and friends.

**Frontend** (single file `src/wishmap/static/index.html`, vanilla JS + Leaflet, inline CSS):
- Boots with `GET /api/me` (401 -> login/register overlay). All API calls go through `API_BASE` (`body[data-api-base]`, default `/api`); the read-only views (public link, static export with `data-api-static="1"`) set `<body class="readonly">`, which hides every edit control via CSS — hide, don't remove DOM nodes.
- Modals: add/edit route, add/edit pin, Garmin login, MFA code, settings (account, Garmin, Strava upload, public link).

**Data flow**: SQLite rows (per user) + `data/users/<id>/` GPX -> `userdata.build` (lazily, cached on `UserState`) -> GeoJSON via `/api/*` (or `/u/<token>/api/*`) -> rendered by Leaflet. Mutations update the DB and swap single features into a warm cache; a sync or Strava upload invalidates it.

**Tests** (`tests/`): `conftest.py` provides `install` (tmp config + data dir), `client`, `user_client` and `second_user_client` (TestClient sessions); `helpers.py` has `ctx()`, `signed_in_client()`, `user_state()`; `fake_garmin.py` stands in for `garminconnect.Garmin` (no network).
