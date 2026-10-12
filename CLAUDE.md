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
uv run wishmap admin --user NAME [--revoke]          # grant/revoke admin rights (server self-update); no --user lists them
uv run wishmap update                                # self-update minus the restart (needs update_dir)
uv run pytest                    # tests (also a pre-commit hook)
uv run pyright src/ tests/       # type-check (src/ is a pre-commit hook; keep tests/ clean too)
```

Never point a dev server, an import or an export at the real `data/` directory for experiments — work on a copy.

## Architecture

Wishmap is a self-hosted, multi-user map app for outdoor sports (hiking, biking, ski touring). Each account has its own pins, GPX routes and ratings on a Leaflet/OpenStreetMap basemap, can sync Garmin Connect activities (named from an uploaded Strava archive), and can publish a read-only public link. `wishmap.toml` holds only server settings; all per-user data is in SQLite plus per-user GPX files. See README.md for the user-facing behaviour, security model and deployment.

**Backend** (FastAPI + uvicorn, Python 3.13), `src/wishmap/`:
- `app.py` — the FastAPI app: `lifespan` (loads settings, migrates the DB, puts an `AppContext` on `app.state.ctx`), every `/api/*` route (pins/routes CRUD, ratings, integrations, sync, MFA, public link), the public `/u/{token}` view and its `/u/{token}/api/*`, routes shared with other accounts (`/api/sharing`, `/api/shared-users`, `/api/shared/{user_id}/routes`: notes and `strava_id` stripped, never in the read-only views), the OSM route overlay lookups (`GET /api/osm-routes/at?sites=…&bbox=w,s,e,n`, a click box ≤ 0.5°, and `GET /api/osm-routes/{site}/{relation_id}`: details + thinned geometry; session only, through `ctx.osm_lookup`), the admin-only self-update (`/api/admin/update`, `…/check`), and the CLI (`main()`: `serve` / `import-toml` / `export` / `admin` / `update` subcommands).
- `auth.py` — cookies (`wishmap_session` Lax, `wishmap_vault` Strict, both HttpOnly), `/api/auth/{register,login,logout,password}`, and the dependencies `Ctx`, `CurrentUser` (session only: 401, or 403 `session_required` for a bearer token), `ApiUser` (session **or** `Authorization: Bearer` API token, scope-checked by method; only the pins/routes/ratings endpoints, via `ApiStateDep` in `app.py`), `AdminUser` (session + `admins` row, else 403 `admin_required`), `confirm_password` (re-asks the password, throttled), `VaultKey` (409 `vault_locked`), `SameOrigin` (CSRF 403; required on **every** mutating endpoint; passes cookie-less token requests).
- `users.py` — accounts (scrypt password hashes) and sessions (sha256 of the token stored); unwraps/re-wraps the vault key on login/password change.
- `vault.py` — per-user AES-GCM vault: key wrapped under a scrypt KEK of the password; `seal`/`unseal` provider secrets (Garmin tokens), bound to user + provider.
- `state.py` — `AppContext` (registry, settings, paths, `LoginThrottle` for logins and another for failed token checks per client address, `OsmLookup`), `StateRegistry`, and `UserState` (cached GeoJSON, sync status/lock, `PendingMfaSlot`). No module globals.
- `userdata.py` — `build()` (DB rows -> pins/routes FeatureCollections; shared with the export) and `ensure_loaded()`, the lazy per-user cache (first read after start/invalidate parses every GPX).
- `schema.py` — the whole SQLite schema plus the idempotent `migrate()` of a legacy single-user DB (old rows go to unclaimed `user_id = 0`). `db.py` — `connect()` (WAL, foreign keys, migrate on every open).
- Store modules, one per table, all keyed on `user_id`: `routes_store.py` (source `web`/`garmin`/`import`, `replace_source` for Garmin sync), `pins_store.py`, `ratings.py`, `integrations_store.py` (settings JSON + sealed tokens), `public_links.py`, `api_tokens.py` (personal API tokens: sha256-stored, scope `read`/`write`, optional expiry, last-used; managed at `/api/tokens` from a session only), `shared_maps.py` (opt-in to share one's routes with every other account), `admins.py` (server admins; granted only by the CLI), `track_cache.py` (each GPX's thinned geometry, stamped with mtime/size, so a restart doesn't re-parse every file).
- `garmin.py` — `sync(GarminSyncContext)`: login from unsealed tokens, else the password given with this sync, else `GarminLoginRequiredError`; MFA parked on the user's `PendingMfaSlot` (`complete_mfa`); downloads GPX to `<user dir>/gpx/garmin/` and replaces the user's Garmin routes. New tokens go out via `on_tokens` (the caller seals them).
- `strava.py` — Strava archive (`.zip`/`activities.csv`) import into `<user dir>/strava/activities.db` and `find_match` for Garmin activities. No Strava API/OAuth.
- Link import (`POST /api/routes/import`): `link_import.resolve()` picks the platform from the pasted link; `ridewithgps.py` (public trip/route via the undocumented `ridewithgps.com/<trips|routes>/<id>.json`), `komoot.py` (public tour/smart tour, or a share link's `share_token`, via the undocumented `api.komoot.de/v007/<tours|smart_tours>/<id>[/coordinates]`) and `waymarked.py` (an OSM route relation from a `<site>.waymarkedtrails.org/#route?id=…` link via its `/api/v1/details/relation/<id>`: walks the nested route, EPSG:3857 -> lon/lat; adds the tag `waymarked`, via `Link.tags`) each parse their links to a digits-only id and fetch from a fixed host; `remote_track.py` has the shared `Track`, errors, bounded same-site download and GPX writer. The track is written as GPX under `gpx/web/`. No API key/OAuth, so private items are refused.
- `osm_lookup.py` — `OsmLookup`, the gatekeeper for the map's Waymarked Trails lookups (`waymarked.list_in` by a mercator bbox snapped by `snap_bbox`, `waymarked.fetch_details`): TTL+LRU caches (lists 1 h, details 24 h), coalescing of identical in-flight calls, a server-wide token bucket, a per-user limit (429), and a circuit breaker that pauses every upstream call for Waymarked's `Retry-After` after a 429/503 (`remote_track.RateLimitedError`; meanwhile 503, cached hits still served). Clock and sleep are replaceable in tests. Waymarked's usage policy: moderate access, no mass downloads — never add prefetching or background refreshes. Outgoing requests carry `remote_track.USER_AGENT`.
- `sync_runner.py` — background sync state machine per `UserState`; invalidates the user's cached map after a successful sync.
- `updater.py` — self-update without root (off unless `update_dir`): `Updater` fetches `update_branch` into `<update_dir>/src`, builds the commit into `releases/<sha>` (uv), swaps the `current`/`previous` symlinks, writes `pending` and SIGTERMs the process; the supervisor restarts it via `deploy/wishmap-launch`, which rolls a release that never starts back to `previous` (`rolled-back`). `Updater.open` (in `lifespan`) settles those markers. Never deploys a tip the running commit already contains.
- `config.py` — `load_settings()` (`ServerSettings`; legacy single-user keys get one warning) and `load_user_toml()` (legacy TOML with `includes`, the importer's input).
- `importer.py` — `import_toml()`: legacy TOML + GPX + unclaimed DB rows + Strava DB + `[garmin]` settings into an account (idempotent).
- `export.py` — `export_site()` static snapshot, and `transform_html()` which bakes the read-only view (also used by `/u/{token}`).
- `models.py` — Pydantic models (settings, `PinConfig`/`RouteConfig`, GeoJSON, request/response bodies). `gpx.py` parses GPX to `[lon, lat]` and thins it to display geometry (`track_parts`: split at gaps, Douglas-Peucker, rounding); `geojson.py` builds features (incl. derived route-start points); `exceptions.py` holds `SyncError` and friends.

**Frontend** (single file `src/wishmap/static/index.html`, vanilla JS + Leaflet, inline CSS):
- Boots with `GET /api/me` (401 -> login/register overlay). All API calls go through `API_BASE` (`body[data-api-base]`, default `/api`); the read-only views (public link, static export with `data-api-static="1"`) set `<body class="readonly">`, which hides every edit control via CSS — hide, don't remove DOM nodes.
- Legend & Filters panel: sport/status selects, a checkbox per tag (`hiddenTags`, in localStorage), other users' shared routes, OSM routes (Waymarked Trails/CyclOSM tile overlays in `osmOverlayPane`; a click on empty map with a Waymarked overlay on looks up routes there — own/shared routes, pins and pin placing tag the DOM event via `markClickTaken` so it skips them; Show draws into `osmHighlightPane`, Add prefills the add-route modal). `matchesFilter()` decides visibility.
- Modals: add/edit route, add/edit pin, Garmin login, MFA code, settings (account, Garmin, Strava upload, sharing, public link, API tokens).

**Data flow**: SQLite rows (per user) + `data/users/<id>/` GPX -> `userdata.build` (lazily, cached on `UserState`) -> GeoJSON via `/api/*` (or `/u/<token>/api/*`) -> rendered by Leaflet. Mutations update the DB and swap single features into a warm cache; a sync or Strava upload invalidates it.

**Tests** (`tests/`): `conftest.py` provides `install` (tmp config + data dir), `client`, `user_client` and `second_user_client` (TestClient sessions); `helpers.py` has `ctx()`, `signed_in_client()`, `user_state()`; `fake_garmin.py` stands in for `garminconnect.Garmin` (no network).
