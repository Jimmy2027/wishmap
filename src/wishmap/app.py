import argparse
import asyncio
import getpass
import logging
import os
import sys
import uuid
import zipfile
from collections.abc import AsyncGenerator, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Annotated

import uvicorn
from fastapi import (
    Body,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from garminconnect import (
    GarminConnectAuthenticationError,
    GarminConnectTooManyRequestsError,
)
from pydantic import BaseModel, Field, ValidationError

from wishmap import (
    api_tokens,
    auth,
    db,
    export,
    garmin,
    gpx,
    integrations_store,
    link_import,
    pins_store,
    public_links,
    ratings,
    remote_track,
    routes_store,
    shared_maps,
    strava,
    sync_runner,
    userdata,
    vault,
)
from wishmap.auth import ApiUser, Ctx, CurrentUser, SameOrigin, VaultKey
from wishmap.config import (
    ConfigError,
    load_settings,
    resolve_config_path,
    resolve_data_root,
)
from wishmap.exceptions import SyncError
from wishmap.garmin import GarminSyncContext
from wishmap.geojson import pin_to_feature, route_features, route_properties
from wishmap.integrations_store import Integration
from wishmap.models import (
    DEFAULT_GARMIN_ACTIVITY_LIMIT,
    MAX_GARMIN_ACTIVITY_LIMIT,
    ApiTokenCreated,
    ApiTokenIn,
    ApiTokenInfo,
    ConfigResponse,
    Feature,
    FeatureCollection,
    FeatureProperties,
    GarminIntegrationResponse,
    GarminSettings,
    IntegrationSummary,
    MeResponse,
    PinConfig,
    PinIn,
    PinPatch,
    PublicLinkResponse,
    RouteAddResponse,
    RouteConfig,
    RouteImportIn,
    RoutePatch,
    RouteRating,
    RouteRatingIn,
    SharedUser,
    SharingIn,
    SharingResponse,
    StravaArchiveResponse,
    SyncRequest,
)
from wishmap.state import AppContext, StateRegistry, UserData, UserState, now_iso

logger = logging.getLogger(__name__)

# Cap on the bytes we pull into memory for an uploaded GPX. gpxpy.parse would
# otherwise read the whole thing into memory; we read(MAX+1) and reject 413
# past the cap so a giant upload can't exhaust the handler's memory. (Starlette
# still spools the raw multipart body to a temp file during request parsing —
# transient, cleaned up at request end, and behind the same-origin guard.)
MAX_GPX_BYTES = 10 * 1024 * 1024  # 10 MB

# Where web-added route GPX files are stored, relative to the user's dir.
WEB_ROUTE_GPX_SUBDIR = Path("gpx") / "web"

# Cap on an uploaded Strava export. Starlette has already spooled the upload
# to a temp file by the time the handler runs; the import then reads only
# activities.csv out of it (itself capped by strava.MAX_CSV_BYTES), so this
# mainly bounds the disk a single request may use.
MAX_STRAVA_ARCHIVE_BYTES = 1024 * 1024 * 1024  # 1 GiB

# The user's Strava activities DB (filled by archive uploads), relative to
# their dir.
STRAVA_DB = Path("strava") / "activities.db"


class MfaRequest(BaseModel):
    code: str = Field(max_length=32)


def _route_logs_through_uvicorn() -> None:
    """Show wishmap's INFO logs under `wishmap serve`.

    uvicorn configures only its own loggers, so ours would fall through to
    the root logger (WARNING, no handler) and every INFO line be dropped.
    Without uvicorn handlers (tests, the CLI) logging is left alone.
    """
    handlers = logging.getLogger("uvicorn").handlers
    log = logging.getLogger("wishmap")
    if handlers and not log.handlers:
        log.handlers = list(handlers)
        log.setLevel(logging.INFO)
        log.propagate = False


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Load settings and set up a fresh AppContext on `app.state.ctx`.

    Map data is NOT built here: each user's GeoJSON is built lazily on the
    first read (userdata.ensure_loaded), so startup stays instant. The
    config is still loaded up front so a broken TOML fails startup loudly.
    """
    _route_logs_through_uvicorn()
    config_path = resolve_config_path()
    try:
        settings = load_settings(config_path)
    except ConfigError as e:
        raise RuntimeError(f"Cannot start wishmap: {e}") from e
    base_path = config_path.parent
    data_root = resolve_data_root(config_path, settings)
    data_root.mkdir(parents=True, exist_ok=True)
    db_path = data_root / "wishmap.db"
    # Every DB open migrates/creates the schema; do one now so a legacy DB
    # is upgraded (or a migration failure surfaces) at startup rather than
    # on some user's first request.
    with db.connect(db_path):
        pass
    app.state.ctx = AppContext(
        registry=StateRegistry(),
        settings=settings,
        config_path=config_path,
        base_path=base_path,
        data_root=data_root,
        db_path=db_path,
    )
    yield


app = FastAPI(lifespan=lifespan)
# /api/routes is several MB of JSON; compressed it is a fraction of that.
# Level 5: nearly level 9's ratio at a fraction of its CPU on every reload.
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)
app.include_router(auth.router)


async def _get_user_state(ctx: Ctx, user: CurrentUser) -> UserState:
    return ctx.registry.get(user.id)


UserStateDep = Annotated[UserState, Depends(_get_user_state)]


async def _get_api_user_state(ctx: Ctx, user: ApiUser) -> UserState:
    return ctx.registry.get(user.id)


# For the endpoints a personal API token may use too (pins, routes,
# ratings; see `auth.api_user`). Everything else takes `UserStateDep`.
ApiStateDep = Annotated[UserState, Depends(_get_api_user_state)]


def _loaded(ctx: AppContext, state: UserState) -> UserData:
    """Return the user's built map data (building it if cold), or 500."""
    try:
        return userdata.ensure_loaded(ctx, state)
    except userdata.UserDataError as e:
        logger.error("Building map data for user %d failed: %s", state.user_id, e)
        raise HTTPException(status_code=500, detail=str(e))


def _user_file(ctx: AppContext, user_id: int, rel: str) -> Path | None:
    """Resolve a route's `gpx` path inside the user's dir; None if it escapes it."""
    user_dir = ctx.user_dir(user_id).resolve()
    path = (user_dir / rel).resolve()
    return path if path.is_relative_to(user_dir) else None


STATIC_DIR = Path(__file__).parent / "static"


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/config")
async def get_config(ctx: Ctx) -> ConfigResponse:
    # Public (the login screen needs it). readonly is False for the app
    # itself; the read-only views (a public link's /u/<token>/api/config,
    # the static export's api/config.json) report True.
    return ConfigResponse(
        title=ctx.settings.title,
        readonly=False,
        registration_open=ctx.settings.allow_registration,
    )


def _public_link_url(request: Request, token: str | None) -> str | None:
    """The absolute URL of a public link, as this request reached the server.

    Behind a reverse proxy, run uvicorn with `--proxy-headers` so the
    scheme and host are the public ones.
    """
    return None if token is None else f"{request.base_url}u/{token}"


@app.get("/api/me")
def get_me(request: Request, ctx: Ctx, user: CurrentUser) -> MeResponse:
    """The signed-in account; 401 tells the web UI to show the login screen."""
    token = public_links.get_token(ctx.db_path, user.id)
    return MeResponse(
        username=user.username,
        public_link=_public_link_url(request, token),
        integrations={
            i.provider: IntegrationSummary(
                username=i.username, logged_in=i.tokens_enc is not None
            )
            for i in integrations_store.get_all(ctx.db_path, user.id)
        },
        vault_unlocked=auth.request_vault_key(request, ctx, user) is not None,
        sharing=shared_maps.is_shared(ctx.db_path, user.id),
    )


# Data GETs are `def` (threadpool): on a cold state the first one blocks on
# the full GPX parse, which must not stall the event loop.
@app.get("/api/pins")
def get_pins(ctx: Ctx, state: ApiStateDep) -> FeatureCollection:
    return _loaded(ctx, state).pins_fc


@app.get("/api/routes")
def get_routes(ctx: Ctx, state: ApiStateDep) -> FeatureCollection:
    return _loaded(ctx, state).routes_fc


def _ratings(ctx: AppContext, user_id: int) -> dict[str, RouteRating]:
    return {
        route_id: RouteRating(**row)
        for route_id, row in ratings.get_all(ctx.db_path, user_id).items()
    }


def _validation_422(e: ValidationError) -> HTTPException:
    """A 422 carrying a model's validation errors in FastAPI's shape."""
    return HTTPException(
        status_code=422,
        detail=[
            {"loc": list(err["loc"]), "msg": err["msg"], "type": err["type"]}
            for err in e.errors()
        ],
    )


def _parse_tags(raw: str) -> list[str]:
    """Split the comma-separated `tags` form field into a clean list."""
    return [t.strip() for t in raw.split(",") if t.strip()]


# Mutation handlers are `def` (threadpool) like the ratings handlers: the
# blocking GPX parse, file write, and sqlite calls run off the event loop.
@app.post("/api/routes", status_code=201, dependencies=[SameOrigin])
def post_route(
    ctx: Ctx,
    state: ApiStateDep,
    gpx_file: UploadFile = File(...),
    strava_url: str = Form(""),
    name: str = Form(...),
    sport: str = Form(...),
    status: str = Form(...),
    tags: str = Form(""),
) -> RouteAddResponse:
    """Add a route from a Strava link + its exported GPX (Approach B).

    Validate everything (origin, size, geometry, Pydantic model) BEFORE
    touching disk or the DB so a bad upload never half-lands. On success:
    write the GPX into the user's dir, record the route, swap it into the
    in-memory map, and return the route + start-pin features so the
    frontend renders it without a refetch.
    """
    # Bounded read — read(MAX+1) so we can detect overflow without slurping
    # an arbitrarily large body into memory.
    raw = gpx_file.file.read(MAX_GPX_BYTES + 1)
    if len(raw) > MAX_GPX_BYTES:
        raise HTTPException(status_code=413, detail="GPX file too large")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=422, detail="GPX is not valid UTF-8 text")

    # Validate geometry first: empty/garbage GPX -> 422, nothing written.
    try:
        coords = gpx.parse_gpx_data(text, "uploaded GPX")
    except Exception:
        raise HTTPException(
            status_code=422, detail="GPX has no parseable track points"
        )

    # Derive a collision-proof, filesystem-safe id. parse_strava_route_id
    # returns digits-only (no path traversal); fall back to a uuid when the
    # URL has no parseable route id.
    route_digits = routes_store.parse_strava_route_id(strava_url)
    route_id = (
        f"web-strava-{route_digits}" if route_digits else f"web-{uuid.uuid4().hex}"
    )
    rel_gpx = WEB_ROUTE_GPX_SUBDIR / f"{route_id}.gpx"

    # Let the Pydantic model be the gate for the remaining fields — an
    # invalid status (not idea/planned/done) raises here, before any write.
    # strava_id is left None: it deep-links Strava *activities*, whereas this
    # is a *route*; the route link lives in notes.
    try:
        route = RouteConfig(
            id=route_id,
            name=name,
            sport=[sport],
            status=status,  # type: ignore[arg-type]  # str coerced to Status enum
            tags=_parse_tags(tags),
            notes=strava_url,
            gpx=rel_gpx.as_posix(),
            strava_id=None,
        )
    except ValidationError as e:
        raise _validation_422(e)

    return _store_web_route(ctx, state, route, text, coords)


def _store_web_route(
    ctx: AppContext,
    state: UserState,
    route: RouteConfig,
    text: str,
    coords: list[list[float]],
) -> RouteAddResponse:
    """Persist a validated web route: its GPX `text`, then its DB row.

    If the DB write fails, don't leave an orphan GPX behind. Then build the
    route's features from `coords` and swap them into the user's map (a
    no-op if it isn't built yet).
    """
    gpx_abs = ctx.user_dir(state.user_id) / route.gpx
    gpx_abs.parent.mkdir(parents=True, exist_ok=True)
    gpx_abs.write_text(text)
    try:
        created = routes_store.insert(ctx.db_path, state.user_id, route)
    except Exception:
        gpx_abs.unlink(missing_ok=True)
        raise

    route_feature, start_feature = route_features(route, coords)
    state.swap_in_route(route_feature, start_feature)

    return RouteAddResponse(
        created=created, route=route_feature, start=start_feature
    )


@app.post("/api/routes/import", status_code=201, dependencies=[SameOrigin])
def import_route(body: RouteImportIn, ctx: Ctx, state: ApiStateDep) -> RouteAddResponse:
    """Add a route fetched from a public link: Ride with GPS or Komoot.

    The platform is picked from the link (`link_import.resolve`). The id
    is derived from the link, so importing the same link again replaces
    the route (`created` False) instead of duplicating it. A blank name
    takes the one the platform has. 422 for a link we can't import (no
    supported platform, private, missing); 502 when the platform can't be
    reached or answers nonsense.
    """
    link = link_import.resolve(body.url)
    if link is None:
        raise HTTPException(
            status_code=422,
            detail=f"Paste a link to {link_import.SUPPORTED}.",
        )
    try:
        track = link.fetch()
    except (remote_track.NotPublicError, remote_track.NotFoundError) as e:
        raise HTTPException(status_code=422, detail=str(e))
    except remote_track.RemoteTrackError as e:
        raise HTTPException(status_code=502, detail=str(e))

    route_id = link.route_id
    notes = track.url
    if track.description:
        notes += "\n\n" + track.description
    try:
        route = RouteConfig(
            id=route_id,
            name=body.name or track.name[:200] or link.fallback_name,
            sport=body.sport,
            status=body.status,
            tags=body.tags,
            notes=notes[:10_000],
            gpx=(WEB_ROUTE_GPX_SUBDIR / f"{route_id}.gpx").as_posix(),
            strava_id=None,
        )
    except ValidationError as e:
        raise _validation_422(e)

    return _store_web_route(ctx, state, route, link.to_gpx(track), track.coords)


@app.delete("/api/routes/{route_id}", status_code=204, dependencies=[SameOrigin])
def delete_route(route_id: str, ctx: Ctx, state: ApiStateDep) -> Response:
    """Delete one of the user's routes + its GPX file, then swap it out of the map.

    Any route the user owns can go; an id they don't own 404s (whether or
    not another user has it).
    """
    route = routes_store.get(ctx.db_path, state.user_id, route_id)
    if route is None:
        raise HTTPException(status_code=404, detail="unknown route_id")
    routes_store.delete(ctx.db_path, state.user_id, route_id)
    # Keep the file if another route still uses it (imports can share one).
    gpx_abs = _user_file(ctx, state.user_id, route.gpx)
    if gpx_abs is not None and not routes_store.gpx_in_use(
        ctx.db_path, state.user_id, route.gpx
    ):
        gpx_abs.unlink(missing_ok=True)
    state.swap_out_route(route_id)
    return Response(status_code=204)


@app.patch("/api/routes/{route_id}", dependencies=[SameOrigin])
def patch_route(
    route_id: str, patch: RoutePatch, ctx: Ctx, state: ApiStateDep
) -> FeatureProperties:
    """Edit one of the user's routes (name, sport, status, tags, notes).

    Fields left out of the body keep their value. Returns the route's new
    feature properties (its start pin carries the same, with the
    `-start` id); the geometry never changes. Any route the user owns is
    editable, but a Garmin sync rewrites its Garmin routes, edits
    included. 404 for an id the user doesn't own.
    """
    try:
        route = routes_store.update(
            ctx.db_path, state.user_id, route_id, patch.model_dump(exclude_unset=True)
        )
    except ValidationError as e:
        raise _validation_422(e)
    if route is None:
        raise HTTPException(status_code=404, detail="unknown route_id")
    def load() -> FeatureProperties | None:
        current = routes_store.get(ctx.db_path, state.user_id, route_id)
        return None if current is None else route_properties(current)

    state.update_route_properties(load)
    return route_properties(route)


# ── Pins ──────────────────────────────────────────────────────────────
# A user's own map markers (route-start pins are derived from routes and
# not editable here). The server picks a new pin's id.


@app.post("/api/pins", status_code=201, dependencies=[SameOrigin])
def post_pin(body: PinIn, ctx: Ctx, state: ApiStateDep) -> Feature:
    """Add a pin; returns its feature so the web UI renders it without a refetch."""
    pin = PinConfig(id=f"pin-{uuid.uuid4().hex}", **body.model_dump())
    pins_store.insert(ctx.db_path, state.user_id, pin)
    _refresh_pin(ctx, state, pin.id)
    return pin_to_feature(pin)


@app.patch("/api/pins/{pin_id}", dependencies=[SameOrigin])
def patch_pin(pin_id: str, patch: PinPatch, ctx: Ctx, state: ApiStateDep) -> Feature:
    """Edit one of the user's pins (fields left out keep their value); 404
    for an id the user doesn't own."""
    try:
        pin = pins_store.update(
            ctx.db_path, state.user_id, pin_id, patch.model_dump(exclude_unset=True)
        )
    except ValidationError as e:
        raise _validation_422(e)
    if pin is None:
        raise HTTPException(status_code=404, detail="unknown pin_id")
    _refresh_pin(ctx, state, pin_id)
    return pin_to_feature(pin)


@app.delete("/api/pins/{pin_id}", status_code=204, dependencies=[SameOrigin])
def delete_pin(pin_id: str, ctx: Ctx, state: ApiStateDep) -> Response:
    """Delete one of the user's pins; 404 for an id the user doesn't own."""
    if not pins_store.delete(ctx.db_path, state.user_id, pin_id):
        raise HTTPException(status_code=404, detail="unknown pin_id")
    state.swap_out_pin(pin_id)
    return Response(status_code=204)


def _refresh_pin(ctx: AppContext, state: UserState, pin_id: str) -> None:
    """Swap pin `pin_id` into the user's map as the DB has it now (see
    `UserState.refresh_pin`)."""

    def load() -> Feature | None:
        pin = pins_store.get(ctx.db_path, state.user_id, pin_id)
        return None if pin is None else pin_to_feature(pin)

    state.refresh_pin(pin_id, load)


# Sync handlers — FastAPI dispatches `def` handlers to a threadpool, so the
# blocking sqlite3 calls don't stall the event loop.
@app.get("/api/ratings")
def get_ratings(ctx: Ctx, state: ApiStateDep) -> dict[str, RouteRating]:
    return _ratings(ctx, state.user_id)


@app.put("/api/ratings/{route_id}", dependencies=[SameOrigin])
def put_rating(
    route_id: str, patch: RouteRatingIn, ctx: Ctx, state: ApiStateDep
) -> RouteRating:
    # Only the user's own routes are ratable; anything else 404s.
    if routes_store.get(ctx.db_path, state.user_id, route_id) is None:
        raise HTTPException(status_code=404, detail="unknown route_id")
    row = ratings.upsert(
        ctx.db_path, state.user_id, route_id, patch.model_dump(exclude_unset=True)
    )
    return RouteRating(**row)


# ── Personal API tokens ───────────────────────────────────────────────
# Managed from a browser session only: a token can neither list nor mint
# tokens (see `api_tokens`).


def _token_info(token: api_tokens.ApiToken) -> ApiTokenInfo:
    return ApiTokenInfo(
        id=token.id,
        name=token.name,
        scope=token.scope,
        created_at=token.created_at,
        last_used_at=token.last_used_at,
        expires_at=token.expires_at,
    )


@app.get("/api/tokens")
def get_api_tokens(ctx: Ctx, user: CurrentUser) -> list[ApiTokenInfo]:
    return [_token_info(t) for t in api_tokens.list_tokens(ctx.db_path, user.id)]


@app.post("/api/tokens", status_code=201, dependencies=[SameOrigin])
def post_api_token(body: ApiTokenIn, ctx: Ctx, user: CurrentUser) -> ApiTokenCreated:
    """Create a token; the response is the only place it appears in the clear.

    422 once the user has `api_tokens.MAX_TOKENS_PER_USER` tokens.
    """
    lifetime = None if body.expires_days is None else timedelta(days=body.expires_days)
    try:
        secret, token = api_tokens.create(
            ctx.db_path, user.id, body.name, body.scope, lifetime
        )
    except api_tokens.TooManyTokensError:
        raise HTTPException(
            status_code=422,
            detail=f"You can have at most {api_tokens.MAX_TOKENS_PER_USER} "
            "API tokens; revoke one first.",
        )
    logger.info("User %d created %s API token %d", user.id, token.scope, token.id)
    return ApiTokenCreated(**_token_info(token).model_dump(), token=secret)


@app.delete("/api/tokens/{token_id}", status_code=204, dependencies=[SameOrigin])
def delete_api_token(token_id: int, ctx: Ctx, user: CurrentUser) -> Response:
    """Revoke one of the user's tokens (it stops working at once); 404 for
    an id the user doesn't own."""
    if not api_tokens.revoke(ctx.db_path, user.id, token_id):
        raise HTTPException(status_code=404, detail="unknown token_id")
    logger.info("User %d revoked API token %d", user.id, token_id)
    return Response(status_code=204)


# ── Public read-only link ─────────────────────────────────────────────
# A user may publish their map at /u/<token>: the same page in its
# read-only form, reading the owner's data from /u/<token>/api/*. The
# token is resolved on every request, so rotating or revoking takes effect
# at once. An unknown (or revoked) token is a plain 404, indistinguishable
# from any other missing page: nothing says whether, or whose, it was.


@app.get("/api/public-link")
def get_public_link(
    request: Request, ctx: Ctx, user: CurrentUser
) -> PublicLinkResponse:
    return PublicLinkResponse(
        url=_public_link_url(request, public_links.get_token(ctx.db_path, user.id))
    )


@app.post("/api/public-link", dependencies=[SameOrigin])
def rotate_public_link(
    request: Request, ctx: Ctx, user: CurrentUser
) -> PublicLinkResponse:
    """Create the user's public link, or replace it (the old URL stops working)."""
    token = public_links.rotate(ctx.db_path, user.id)
    logger.info("User %d published a new public link", user.id)
    return PublicLinkResponse(url=_public_link_url(request, token))


@app.delete("/api/public-link", status_code=204, dependencies=[SameOrigin])
def revoke_public_link(ctx: Ctx, user: CurrentUser) -> Response:
    """Take the user's public link down. Idempotent."""
    if public_links.revoke(ctx.db_path, user.id):
        logger.info("User %d revoked their public link", user.id)
    return Response(status_code=204)


def _public_owner(token: str, ctx: Ctx) -> UserState:
    """The state of the user a public link token belongs to; 404 if none."""
    user_id = public_links.resolve(ctx.db_path, token)
    if user_id is None:
        raise HTTPException(status_code=404)
    return ctx.registry.get(user_id)


PublicOwner = Annotated[UserState, Depends(_public_owner)]

# Sent with the public page: send only the origin as Referer, which keeps
# the token out of the page's requests to tile servers/CDNs and of links
# followed from it (no-referrer would get the tiles blocked: OSM's tile
# policy requires a Referer), and keep the page out of search engines
# should the URL leak.
_PUBLIC_PAGE_HEADERS = {"Referrer-Policy": "strict-origin", "X-Robots-Tag": "noindex"}


@app.get("/u/{token}", response_class=HTMLResponse)
def public_page(token: str, state: PublicOwner) -> HTMLResponse:
    html = export.transform_html(
        (STATIC_DIR / "index.html").read_text(),
        api_base=f"/u/{token}/api",
        static=False,
    )
    return HTMLResponse(html, headers=_PUBLIC_PAGE_HEADERS)


@app.get("/u/{token}/api/config")
def public_config(ctx: Ctx, state: PublicOwner) -> ConfigResponse:
    return ConfigResponse(title=ctx.settings.title, readonly=True)


@app.get("/u/{token}/api/pins")
def public_pins(ctx: Ctx, state: PublicOwner) -> FeatureCollection:
    return _loaded(ctx, state).pins_fc


@app.get("/u/{token}/api/routes")
def public_routes(ctx: Ctx, state: PublicOwner) -> FeatureCollection:
    return _loaded(ctx, state).routes_fc


@app.get("/u/{token}/api/ratings")
def public_ratings(ctx: Ctx, state: PublicOwner) -> dict[str, RouteRating]:
    return _ratings(ctx, state.user_id)


# ── Routes shared with the other accounts ─────────────────────────────
# A user may opt in to share their routes with every other account on the
# server, which can then overlay them on its own map. Only the routes go
# out, stripped of notes and Strava ids; ratings and pins stay private.
# The opt-in is checked on every request, so opting out takes effect at
# once. A user who doesn't share (or doesn't exist) is a plain 404. The
# read-only views (/u/<token>, the static export) never show shared routes.


@app.put("/api/sharing", dependencies=[SameOrigin])
def put_sharing(body: SharingIn, ctx: Ctx, user: CurrentUser) -> SharingResponse:
    """Share the user's routes with every other account, or stop. Idempotent."""
    shared_maps.set_shared(ctx.db_path, user.id, body.enabled)
    logger.info(
        "User %d %s sharing their routes",
        user.id,
        "enabled" if body.enabled else "disabled",
    )
    return SharingResponse(enabled=body.enabled)


@app.get("/api/shared-users")
def get_shared_users(ctx: Ctx, user: CurrentUser) -> list[SharedUser]:
    """The other accounts that share their routes, by name."""
    return [
        SharedUser(id=uid, username=name)
        for uid, name in shared_maps.list_shared(ctx.db_path, exclude_user_id=user.id)
    ]


def _shared_owner(user_id: int, ctx: Ctx, user: CurrentUser) -> UserState:
    """The state of another user who shares their routes; 404 otherwise."""
    if user_id == user.id or not shared_maps.is_shared(ctx.db_path, user_id):
        raise HTTPException(status_code=404)
    return ctx.registry.get(user_id)


SharedOwner = Annotated[UserState, Depends(_shared_owner)]


def _shared_view(fc: FeatureCollection) -> FeatureCollection:
    """The routes as another user sees them: no notes, no Strava id.

    The ids are replaced by positions too: a synced route's id is
    `garmin-<activity id>`, which would point at the owner's Garmin
    Connect activity. The viewer never addresses a shared route by id.

    Copies only the properties (the geometry objects are shared), so the
    owner's cached collection is never mutated.
    """
    return FeatureCollection(
        features=[
            f.model_copy(
                update={
                    "properties": f.properties.model_copy(
                        update={"id": f"r{i}", "notes": "", "strava_id": None}
                    )
                }
            )
            for i, f in enumerate(fc.features)
        ]
    )


@app.get("/api/shared/{user_id}/routes")
def get_shared_routes(ctx: Ctx, state: SharedOwner) -> FeatureCollection:
    return _shared_view(_loaded(ctx, state).routes_fc)


# ── Garmin / Strava integrations and the sync ─────────────────────────
# A user's Garmin account and sync settings live in their `integrations`
# row; its session tokens only sealed with the user's vault key (see
# `vault`), so everything that reads or writes them depends on `VaultKey`.
# The Garmin password itself is never stored: it arrives with one POST
# /api/sync, is used for that run's login and then dropped.


def _garmin_settings(integration: Integration | None) -> tuple[int, list[str]]:
    """The (activity_limit, sport_filter) stored for Garmin, or the defaults."""
    settings = {} if integration is None else integration.settings
    limit = settings.get("activity_limit")
    if not isinstance(limit, int) or not 1 <= limit <= MAX_GARMIN_ACTIVITY_LIMIT:
        limit = DEFAULT_GARMIN_ACTIVITY_LIMIT
    raw_filter = settings.get("sport_filter")
    sport_filter = (
        [s for s in raw_filter if isinstance(s, str)]
        if isinstance(raw_filter, list)
        else []
    )
    return limit, sport_filter


def _garmin_response(integration: Integration | None) -> GarminIntegrationResponse:
    limit, sport_filter = _garmin_settings(integration)
    return GarminIntegrationResponse(
        username=None if integration is None else integration.username,
        activity_limit=limit,
        sport_filter=sport_filter,
        logged_in=integration is not None and integration.tokens_enc is not None,
    )


def _unseal_garmin_tokens(
    ctx: AppContext, integration: Integration, key: bytes, user_id: int
) -> str | None:
    """The stored Garmin tokens in the clear, or None if there are none.

    `key` is the user's verified vault key, so tokens that don't open with
    it are unusable leftovers: they are forgotten and Garmin asks for a
    login again, rather than failing every sync.
    """
    if integration.tokens_enc is None:
        return None
    try:
        return vault.unseal(
            key, integration.tokens_enc, user_id=user_id, provider="garmin"
        ).decode("utf-8")
    except (vault.VaultError, UnicodeDecodeError):
        logger.warning(
            "Stored Garmin tokens of user %d do not open; forgetting them", user_id
        )
        integrations_store.set_tokens(ctx.db_path, user_id, "garmin", None)
        return None


def _garmin_token_saver(
    ctx: AppContext, user_id: int, key: bytes, garmin_username: str
) -> Callable[[str], None]:
    """Seal Garmin tokens with the vault key and store them for the account."""

    def save(tokens: str) -> None:
        sealed = vault.seal(key, tokens.encode("utf-8"), user_id=user_id, provider="garmin")
        # Only onto the account the tokens are for: a user who switched
        # Garmin accounts mid-sync keeps their (token-less) new one.
        if not integrations_store.set_tokens(
            ctx.db_path, user_id, "garmin", sealed, for_username=garmin_username
        ):
            logger.info(
                "Garmin account of user %d changed during login; tokens dropped",
                user_id,
            )

    return save


def _garmin_sync_context(
    ctx: AppContext, state: UserState, key: bytes, password: str | None
) -> GarminSyncContext | None:
    """Build the user's Garmin sync context; None without a Garmin account."""
    integration = integrations_store.get(ctx.db_path, state.user_id, "garmin")
    if integration is None or not integration.username:
        return None
    limit, sport_filter = _garmin_settings(integration)
    user_dir = ctx.user_dir(state.user_id)
    return GarminSyncContext(
        user_id=state.user_id,
        db_path=ctx.db_path,
        user_dir=user_dir,
        username=integration.username,
        activity_limit=limit,
        sport_filter=sport_filter,
        tokens_json=_unseal_garmin_tokens(ctx, integration, key, state.user_id),
        password=password,
        strava_db=user_dir / STRAVA_DB,
        pending=state.pending_mfa,
        on_tokens=_garmin_token_saver(ctx, state.user_id, key, integration.username),
    )


@app.get("/api/integrations/garmin")
def get_garmin_integration(ctx: Ctx, state: UserStateDep) -> GarminIntegrationResponse:
    """The user's Garmin account and sync settings (defaults if none is set)."""
    return _garmin_response(integrations_store.get(ctx.db_path, state.user_id, "garmin"))


@app.put("/api/integrations/garmin", dependencies=[SameOrigin])
def put_garmin_integration(
    settings: GarminSettings, ctx: Ctx, state: UserStateDep
) -> GarminIntegrationResponse:
    """Set the user's Garmin account and sync settings.

    Switching to another Garmin account forgets the old account's saved
    login (and any login waiting for its MFA code); the next sync asks for
    the new account's password.
    """
    before = integrations_store.get(ctx.db_path, state.user_id, "garmin")
    integration = integrations_store.upsert_settings(
        ctx.db_path,
        state.user_id,
        "garmin",
        username=settings.username,
        settings={
            "activity_limit": settings.activity_limit,
            "sport_filter": settings.sport_filter,
        },
    )
    if before is None or before.username != settings.username:
        state.pending_mfa.clear()
    return _garmin_response(integration)


@app.delete("/api/integrations/garmin", status_code=204, dependencies=[SameOrigin])
def delete_garmin_login(ctx: Ctx, state: UserStateDep) -> Response:
    """Forget the saved Garmin login (the account and settings stay).

    The next sync asks for the Garmin password again. Idempotent.
    """
    integrations_store.set_tokens(ctx.db_path, state.user_id, "garmin", None)
    state.pending_mfa.clear()
    return Response(status_code=204)


@app.post("/api/integrations/strava/archive", dependencies=[SameOrigin])
async def post_strava_archive(
    ctx: Ctx, state: UserStateDep, archive: UploadFile = File(...)
) -> StravaArchiveResponse:
    """Import a Strava data export (the .zip, or its activities.csv).

    Fills the user's Strava activities DB, then re-matches their Garmin
    routes against it, so Strava names and sports show up without a new
    Garmin sync (when no sync has run yet, the first one does the
    matching). 409 while a sync is running — both rewrite the Garmin
    routes. 413 for an oversized upload, 422 for an unreadable export.
    """
    if sync_runner.is_running(state):
        raise HTTPException(status_code=409, detail="sync_running")
    # Held for the whole import: a sync started meanwhile waits for it in
    # run_sync, then matches against the new DB, instead of interleaving
    # its route rewrite with the re-match below. (No await between the
    # check above and taking the lock, so no sync can begin in between.)
    async with state.sync_lock:
        return await asyncio.to_thread(_import_strava_archive, ctx, state, archive)


def _import_strava_archive(
    ctx: AppContext, state: UserState, archive: UploadFile
) -> StravaArchiveResponse:
    """The blocking body of post_strava_archive (runs under the sync lock)."""
    upload = archive.file
    upload.seek(0, os.SEEK_END)
    if upload.tell() > MAX_STRAVA_ARCHIVE_BYTES:
        raise HTTPException(status_code=413, detail="Strava export too large")
    user_dir = ctx.user_dir(state.user_id)
    strava_db = user_dir / STRAVA_DB
    try:
        imported = strava.import_archive(strava_db, upload)
    except SyncError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except zipfile.BadZipFile:
        raise HTTPException(status_code=422, detail="The upload is not a valid .zip")
    total = strava.count_activities(strava_db)
    integrations_store.upsert_settings(
        ctx.db_path,
        state.user_id,
        "strava",
        username=None,
        settings={"activities": total, "imported_at": now_iso()},
    )
    matched = garmin.rematch_strava(ctx.db_path, state.user_id, user_dir, strava_db)
    if matched is not None:
        state.invalidate()
    logger.info(
        "User %d imported %d Strava activities (%s Garmin routes matched)",
        state.user_id,
        imported,
        "no" if matched is None else matched,
    )
    return StravaArchiveResponse(imported=imported, total=total, matched_routes=matched)


@app.post("/api/sync", dependencies=[SameOrigin])
async def post_sync(
    ctx: Ctx,
    state: UserStateDep,
    key: VaultKey,
    body: Annotated[SyncRequest | None, Body()] = None,
) -> JSONResponse:
    """Start a background Garmin sync of the user's account.

    Returns 202 with current status when a new sync starts; 409 with the
    current status when one is already running. Idempotent under spam:
    repeated clicks while running just keep returning the same status.
    The optional `garmin_password` is for a fresh Garmin login, when the
    last status asked for one (`needs_garmin_login`).
    """
    if sync_runner.is_running(state):
        return JSONResponse(status_code=409, content=sync_runner.get_status(state))
    password = (body.garmin_password if body is not None else None) or None
    garmin_ctx = await asyncio.to_thread(_garmin_sync_context, ctx, state, key, password)
    if not sync_runner.begin(state):
        return JSONResponse(status_code=409, content=sync_runner.get_status(state))
    asyncio.create_task(sync_runner.run_sync(state, garmin_ctx))
    return JSONResponse(status_code=202, content=sync_runner.get_status(state))


@app.get("/api/sync/status")
async def get_sync_status(state: UserStateDep) -> dict[str, object]:
    return sync_runner.get_status(state)


@app.post("/api/garmin/mfa", dependencies=[SameOrigin])
async def post_garmin_mfa(
    req: MfaRequest, ctx: Ctx, state: UserStateDep, key: VaultKey
) -> Response:
    """Finish a Garmin login that the user's sync left waiting for an MFA code.

    On 204 the new tokens are sealed and saved; the web UI then re-POSTs
    /api/sync, which logs in from them. A wrong code (401) keeps the
    pending login so the user can retry.
    """
    integration = await asyncio.to_thread(
        integrations_store.get, ctx.db_path, state.user_id, "garmin"
    )
    if integration is None or not integration.username:
        return JSONResponse(status_code=400, content={"reason": "no_garmin_config"})
    if not state.pending_mfa.is_set():
        return JSONResponse(status_code=409, content={"reason": "no_pending_login"})
    try:
        tokens = await asyncio.to_thread(
            garmin.complete_mfa, state.pending_mfa, req.code, integration.username
        )
    except GarminConnectAuthenticationError as e:
        return JSONResponse(
            status_code=401, content={"reason": "bad_code", "detail": str(e)}
        )
    except GarminConnectTooManyRequestsError as e:
        return JSONResponse(
            status_code=429, content={"reason": "rate_limited", "detail": str(e)}
        )
    except SyncError as e:
        return JSONResponse(
            status_code=409, content={"reason": "no_pending_login", "detail": str(e)}
        )
    except Exception as e:
        logger.exception("Garmin MFA completion failed")
        return JSONResponse(
            status_code=500, content={"reason": "unknown", "detail": str(e)}
        )
    if tokens is None:
        return JSONResponse(
            status_code=500,
            content={
                "reason": "unknown",
                "detail": "Garmin accepted the code but returned no session to save.",
            },
        )
    save = _garmin_token_saver(ctx, state.user_id, key, integration.username)
    await asyncio.to_thread(save, tokens)
    return Response(status_code=204)


def _add_config_option(parser: argparse.ArgumentParser, default: str | None) -> None:
    parser.add_argument(
        "--config",
        default=default,
        metavar="TOML",
        help="server config (default: $WISHMAP_CONFIG or ./wishmap.toml)",
    )


def _add_serve_options(parser: argparse.ArgumentParser, *, defaults: bool) -> None:
    # The options exist both before and after `serve`; only the top-level
    # copy carries defaults, so `wishmap --port 1 serve` keeps its port.
    parser.add_argument(
        "--host",
        default="127.0.0.1" if defaults else argparse.SUPPRESS,
        help="interface to bind (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000 if defaults else argparse.SUPPRESS,
        help="port to listen on (default: 8000)",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wishmap",
        description="Wishmap: a self-hosted map of outdoor routes and pins.",
    )
    _add_config_option(parser, None)
    _add_serve_options(parser, defaults=True)
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")

    serve = commands.add_parser(
        "serve", help="run the web server (the default without a command)"
    )
    _add_config_option(serve, argparse.SUPPRESS)
    _add_serve_options(serve, defaults=False)

    import_cmd = commands.add_parser(
        "import-toml",
        help="import a single-user wishmap.toml (and its data) into an account",
        description=(
            "Import the pins and routes of a single-user wishmap TOML (with "
            "its includes and GPX files), the routes and ratings the "
            "pre-account database left unclaimed, the Strava activities DB "
            "and the [garmin] settings into an account, creating the account "
            "if needed. Safe to re-run: the TOML's routes and pins replace "
            "the account's ones with the same id."
        ),
    )
    _add_config_option(import_cmd, argparse.SUPPRESS)
    import_cmd.add_argument("--user", required=True, help="account to import into")
    import_cmd.add_argument(
        "--password-stdin",
        action="store_true",
        help=(
            "read a new account's password from the first line of stdin "
            "(default: $WISHMAP_PASSWORD, else prompt)"
        ),
    )
    import_cmd.add_argument(
        "--claim",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "take over the routes and ratings the pre-account database left "
            "unclaimed (default: only when importing the server's own config)"
        ),
    )
    import_cmd.add_argument("toml", type=Path, help="the wishmap.toml to import")

    export_cmd = commands.add_parser(
        "export", help="write a static read-only snapshot of an account's map"
    )
    _add_config_option(export_cmd, argparse.SUPPRESS)
    export_cmd.add_argument("--user", required=True, help="account to export")
    export_cmd.add_argument("dir", type=Path, help="output directory")
    return parser


def _password_reader(username: str, from_stdin: bool) -> Callable[[], str]:
    """How `import-toml` gets a new account's password (asked only if needed)."""

    def read() -> str:
        if from_stdin:
            return sys.stdin.readline().rstrip("\r\n")
        env = os.environ.get("WISHMAP_PASSWORD")
        if env:
            return env
        password = getpass.getpass(f"Password for the new account {username!r}: ")
        if getpass.getpass("Repeat the password: ") != password:
            from wishmap.importer import ImportTomlError

            raise ImportTomlError("The passwords do not match")
        return password

    return read


def _run_import(args: argparse.Namespace) -> None:
    from wishmap.importer import count_unclaimed, import_toml

    config_path = resolve_config_path()
    own_config = config_path.resolve() == args.toml.resolve()
    # Importing the server's own TOML: its pins/routes are what's being
    # moved into the account, so skip the "import them" advice.
    settings = load_settings(config_path, warn_legacy=not own_config)
    data_root = resolve_data_root(config_path, settings)
    db_path = data_root / "wishmap.db"
    # The unclaimed rows are the server's own pre-account install: another
    # TOML (another person's install) must not take them by default.
    claim: bool = own_config if args.claim is None else args.claim
    result = import_toml(
        args.toml,
        args.user,
        db_path=db_path,
        data_root=data_root,
        legacy_base=config_path.parent,
        get_password=_password_reader(args.user, args.password_stdin),
        claim=claim,
    )
    routes = ", ".join(f"{n} {source}" for source, n in sorted(result.routes.items()))
    print(
        f"Imported {args.toml} into account {result.username!r}"
        f"{' (created)' if result.user_created else ''}:"
    )
    print(f"  pins: {result.pins}")
    print(f"  routes: {routes or 'none'} ({result.new_routes} new)")
    print(f"  GPX files copied: {result.gpx_copied}")
    if claim:
        print(
            f"  claimed from before accounts: {result.claimed_routes} route(s), "
            f"{result.claimed_ratings} rating(s), {result.claimed_pins} pin(s)"
        )
        if result.unclaimed_conflicts:
            print(
                f"  left unclaimed (the account already has their id): "
                f"{result.unclaimed_conflicts} row(s)"
            )
    elif unclaimed := count_unclaimed(db_path):
        print(
            f"  left unclaimed from before accounts: {unclaimed} row(s) "
            f"(import the server's config, or pass --claim, to take them)"
        )
    if result.strava_activities is not None:
        print(f"  Strava activities: {result.strava_activities}")
    if result.garmin_username is not None:
        print(f"  Garmin account: {result.garmin_username}")


def _run_export(args: argparse.Namespace) -> None:
    for path in export.export_site(args.dir, args.user):
        print(f"wrote {path}")


def main(argv: Sequence[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    if args.config:
        os.environ["WISHMAP_CONFIG"] = args.config

    if args.command in (None, "serve"):
        uvicorn.run("wishmap.app:app", host=args.host, port=args.port, reload=True)
        return

    # CLI commands get logging config here; the server inherits uvicorn's.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    # Lazy import (as in the helpers above): the CLI-only importer stays
    # off the server path.
    from wishmap.importer import ImportTomlError

    try:
        if args.command == "import-toml":
            _run_import(args)
        else:
            _run_export(args)
    except (ConfigError, ImportTomlError, export.ExportError) as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
