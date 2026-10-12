from enum import Enum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Status(str, Enum):
    IDEA = "idea"
    PLANNED = "planned"
    DONE = "done"


class PinConfig(BaseModel):
    id: str
    name: str
    lat: float
    lon: float
    sport: list[str]
    status: Status
    tags: list[str] = []
    notes: str = ""

    @field_validator("sport", mode="before")
    @classmethod
    def normalize_sport(cls, v: str | list[str]) -> list[str]:
        if isinstance(v, str):
            return [v]
        return v


class RouteConfig(BaseModel):
    id: str
    name: str
    sport: list[str]
    status: Status
    tags: list[str] = []
    notes: str = ""
    gpx: str
    strava_id: int | None = None

    @field_validator("sport", mode="before")
    @classmethod
    def normalize_sport(cls, v: str | list[str]) -> list[str]:
        if isinstance(v, str):
            return [v]
        return v


# Garmin sync defaults/limits (an account's `integrations.settings`).
DEFAULT_GARMIN_ACTIVITY_LIMIT = 1000
MAX_GARMIN_ACTIVITY_LIMIT = 20000


class GarminConfig(BaseModel):
    """The [garmin] section of a legacy single-user TOML (read by the import).

    Only the account name and sync settings carry over; anything else the
    section holds (a password, a tokenstore path, …) is ignored.
    """

    username: str
    activity_limit: int = DEFAULT_GARMIN_ACTIVITY_LIMIT
    sport_filter: list[str] = []


class StravaConfig(BaseModel):
    """The [strava] section of a legacy single-user TOML (read by the import).

    Only where its activities DB lives matters now; the old API settings
    (client id/secret, tokenstore) are ignored.
    """

    gpx_dir: str = "data/strava"


class ServerSettings(BaseModel):
    """Server-wide settings: the `wishmap.toml` the server is started with.

    Per-user data (pins, routes, integrations) lives in the database, not
    here; `config.load_settings` warns about legacy single-user keys.
    """

    title: str = "wishmap"
    # Open self-registration. Off = only existing accounts can log in.
    allow_registration: bool = True
    # Mark auth cookies `Secure`. Turn on when served over HTTPS (the
    # cookies are then never sent over plain HTTP).
    secure_cookies: bool = False
    # Lifetime of a login session (and its cookie), in days.
    session_days: int = Field(default=30, ge=1, le=365)
    # App-managed state (`wishmap.db`, per-user files), relative to the
    # config file's directory unless absolute.
    data_dir: str = "data"
    # Self-update from the web UI (see `updater`): the directory, owned by
    # the server's user, with the git clone and the release venvs. Relative
    # to the config file's directory unless absolute. Unset = off.
    update_dir: str | None = None
    # The branch an update deploys the newest commit of.
    update_branch: str = Field(default="main", pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
    # Deploy only commits `git verify-commit` accepts (keys in <update_dir>/gnupg).
    update_require_signed: bool = False


class WishmapConfig(BaseModel):
    """A legacy single-user TOML: pins/routes plus the [garmin]/[strava] sections.

    No longer the server's config (see `ServerSettings`); only read as the
    input of a TOML import.
    """

    title: str = "wishmap"
    includes: list[str] = []
    garmin: GarminConfig | None = None
    strava: StravaConfig | None = None
    pins: list[PinConfig] = []
    routes: list[RouteConfig] = []


class FeatureProperties(BaseModel):
    id: str
    name: str
    kind: Literal["pin", "route", "route_start"]
    sport: list[str]
    status: str
    tags: list[str]
    notes: str
    strava_id: int | None = None


class PointGeometry(BaseModel):
    type: Literal["Point"] = "Point"
    coordinates: list[float]


class LineStringGeometry(BaseModel):
    type: Literal["LineString"] = "LineString"
    coordinates: list[list[float]]


class MultiLineStringGeometry(BaseModel):
    """A route whose track jumps (a gap in the recording) is drawn in parts."""

    type: Literal["MultiLineString"] = "MultiLineString"
    coordinates: list[list[list[float]]]


class Feature(BaseModel):
    type: Literal["Feature"] = "Feature"
    geometry: PointGeometry | LineStringGeometry | MultiLineStringGeometry
    properties: FeatureProperties


class FeatureCollection(BaseModel):
    type: Literal["FeatureCollection"] = "FeatureCollection"
    features: list[Feature]


class ConfigResponse(BaseModel):
    title: str
    # Read-only mode is a property of the *served view*, not the data: the
    # app's own /api/config reports False; a public link's config and the
    # static export baked by `wishmap export` report True. Defaults False
    # so the app's /api/config shape is unchanged (regression contract).
    readonly: bool = False
    # Whether the login screen offers "Register" (ServerSettings).
    registration_open: bool = False


class Credentials(BaseModel):
    """POST body of /api/auth/register and /api/auth/login.

    Only length-capped here; `users.create_user` owns the username and
    password rules, so a login with an odd username is a plain 401.
    """

    username: str = Field(max_length=64)
    password: str = Field(max_length=1024)


class PasswordChange(BaseModel):
    """POST body of /api/auth/password."""

    current_password: str = Field(max_length=1024)
    new_password: str = Field(max_length=1024)


class AccountResponse(BaseModel):
    """Who a register/login just signed in as."""

    username: str


class IntegrationSummary(BaseModel):
    """One connected service, minus anything secret."""

    username: str | None
    # True when provider tokens are stored (a sync needs no password).
    logged_in: bool


class MeResponse(BaseModel):
    """GET /api/me: the signed-in account."""

    username: str
    # Absolute URL of the user's public read-only map, if they made one.
    public_link: str | None
    integrations: dict[str, IntegrationSummary]
    # Whether this browser holds the vault key (see `vault`); without it,
    # saved provider logins can't be used until the user logs in again.
    vault_unlocked: bool
    # Whether the user shares their routes with the server's other accounts.
    sharing: bool
    # Whether the user may update the server (see `admins`).
    is_admin: bool


class GarminSettings(BaseModel):
    """PUT body of /api/integrations/garmin: the account and what to sync."""

    username: str = Field(min_length=1, max_length=254)
    # How many of the newest activities a sync puts on the map.
    activity_limit: int = Field(
        default=DEFAULT_GARMIN_ACTIVITY_LIMIT, ge=1, le=MAX_GARMIN_ACTIVITY_LIMIT
    )
    # Garmin activity type keys (e.g. "hiking"); empty = every type.
    sport_filter: list[str] = Field(default=[], max_length=100)

    @field_validator("username", mode="after")
    @classmethod
    def strip_username(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("username must not be blank")
        return v

    @field_validator("sport_filter", mode="after")
    @classmethod
    def clean_sport_filter(cls, v: list[str]) -> list[str]:
        cleaned: list[str] = []
        for key in (k.strip() for k in v):
            if len(key) > 64:
                raise ValueError("sport_filter entries are at most 64 characters")
            if key and key not in cleaned:
                cleaned.append(key)
        return cleaned


class GarminIntegrationResponse(BaseModel):
    """GET/PUT /api/integrations/garmin: settings plus login state, no secrets."""

    # None until the user saved a Garmin account.
    username: str | None
    activity_limit: int
    sport_filter: list[str]
    # True when Garmin session tokens are stored (a sync needs no password).
    logged_in: bool


class SyncRequest(BaseModel):
    """Optional POST body of /api/sync.

    `garmin_password` is used once, for a fresh Garmin login when no saved
    session works, and never stored or logged.
    """

    garmin_password: str | None = Field(default=None, max_length=1024)


class StravaArchiveResponse(BaseModel):
    """POST /api/integrations/strava/archive: what the import did."""

    # Activities read from the upload (inserted or refreshed).
    imported: int
    # Activities in the user's Strava DB afterwards.
    total: int
    # Garmin routes now matched to a Strava activity; None when there is
    # nothing to match yet (no Garmin sync has run for this account).
    matched_routes: int | None


class RouteAddResponse(BaseModel):
    """201 response for POST /api/routes.

    `created` is True for a brand-new route, False when an existing web
    route with the same id was replaced (re-adding the same Strava route).
    `route` is the LineString feature and `start` its route-start Point.
    The frontend appends both to its in-memory collections and re-renders
    without refetching.
    """
    created: bool
    route: Feature
    start: Feature


def _clean_tags(v: list[str] | None) -> list[str] | None:
    """Strip tags, drop empty and duplicate ones; cap each at 64 characters."""
    if v is None:
        return None
    cleaned: list[str] = []
    for tag in (t.strip() for t in v):
        if len(tag) > 64:
            raise ValueError("tags are at most 64 characters")
        if tag and tag not in cleaned:
            cleaned.append(tag)
    return cleaned


def _clean_sport(v: list[str] | None) -> list[str] | None:
    """Strip sports and drop empty/duplicate ones; at least one must remain."""
    if v is None:
        return None
    cleaned: list[str] = []
    for sport in (s.strip() for s in v):
        if len(sport) > 64:
            raise ValueError("sports are at most 64 characters")
        if sport and sport not in cleaned:
            cleaned.append(sport)
    if not cleaned:
        raise ValueError("at least one sport is required")
    return cleaned


class _FeatureFieldRules(BaseModel):
    """Validation shared by the route/pin edit bodies (no fields of its own).

    Names are stripped and must not be blank; `sport` may be a single
    string and must keep at least one entry; sports and tags are stripped
    and de-duplicated. Unknown fields are refused (422), so an attempt to
    change something that isn't editable (an id, a GPX path) fails loudly
    instead of being silently dropped.
    """

    model_config = ConfigDict(extra="forbid")

    @field_validator("sport", mode="before", check_fields=False)
    @classmethod
    def sport_to_list(cls, v: object) -> object:
        return [v] if isinstance(v, str) else v

    @field_validator("name", mode="after", check_fields=False)
    @classmethod
    def strip_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            raise ValueError("name must not be blank")
        return v

    @field_validator("sport", mode="after", check_fields=False)
    @classmethod
    def clean_sport(cls, v: list[str] | None) -> list[str] | None:
        return _clean_sport(v)

    @field_validator("tags", mode="after", check_fields=False)
    @classmethod
    def clean_tags(cls, v: list[str] | None) -> list[str] | None:
        return _clean_tags(v)


class RoutePatch(_FeatureFieldRules):
    """PATCH body of /api/routes/{id}; the GPX (geometry) is not editable.

    A field that is absent keeps its stored value (handlers pass
    `model_dump(exclude_unset=True)` on). An explicit null is rejected:
    no field can be cleared to nothing — send "" or [] for notes or tags.
    """

    name: str | None = Field(default=None, max_length=200)
    sport: list[str] | None = Field(default=None, max_length=20)
    status: Status | None = None
    tags: list[str] | None = Field(default=None, max_length=50)
    notes: str | None = Field(default=None, max_length=10_000)

    @model_validator(mode="after")
    def no_nulls(self) -> Self:
        nulls = sorted(f for f in self.model_fields_set if getattr(self, f) is None)
        if nulls:
            raise ValueError(f"must not be null: {', '.join(nulls)}")
        return self


class PinPatch(RoutePatch):
    """PATCH body of /api/pins/{id}: the route's fields plus the position."""

    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)


class PinIn(_FeatureFieldRules):
    """POST body of /api/pins: a new pin (the server picks its id)."""

    name: str = Field(max_length=200)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    sport: list[str] = Field(max_length=20)
    status: Status
    tags: list[str] = Field(default=[], max_length=50)
    notes: str = Field(default="", max_length=10_000)


class RouteImportIn(_FeatureFieldRules):
    """POST body of /api/routes/import: a route fetched from a link.

    `name` may be left out to take the name the platform has for it.
    """

    url: str = Field(max_length=2000)
    name: str | None = Field(default=None, max_length=200)
    sport: list[str] = Field(max_length=20)
    status: Status
    tags: list[str] = Field(default=[], max_length=50)


class PublicLinkResponse(BaseModel):
    """GET/POST /api/public-link: the user's public read-only map, if any."""

    # Absolute URL of the read-only map; None while the user has no link.
    url: str | None


class SharingIn(BaseModel):
    """PUT body of /api/sharing: share routes with every other account, or not."""

    enabled: bool


class SharingResponse(BaseModel):
    """PUT /api/sharing: whether the user now shares their routes."""

    enabled: bool


class SharedUser(BaseModel):
    """GET /api/shared-users: one account that shares its routes."""

    id: int
    username: str


class ApiTokenIn(BaseModel):
    """POST body of /api/tokens: a new personal API token.

    `read` may GET the pins, routes and ratings; `write` may also change
    them. `expires_days` None = the token never expires.
    """

    name: str = Field(min_length=1, max_length=100)
    scope: Literal["read", "write"]
    expires_days: int | None = Field(default=None, ge=1, le=3650)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name must not be blank")
        return v


class ApiTokenInfo(BaseModel):
    """GET /api/tokens: one of the user's tokens (never the secret)."""

    id: int
    name: str
    scope: Literal["read", "write"]
    created_at: str
    last_used_at: str | None
    # None = never expires.
    expires_at: str | None


class ApiTokenCreated(ApiTokenInfo):
    """POST /api/tokens: the new token — the only time it is shown."""

    token: str


class RouteRatingIn(BaseModel):
    """PUT body. Any subset of axes may be sent.

    Omitted axes preserve their stored value; explicit None clears that axis.
    """
    fun: int | None = Field(default=None, ge=1, le=5)
    difficulty: int | None = Field(default=None, ge=1, le=5)
    scenery: int | None = Field(default=None, ge=1, le=5)


class RouteRating(RouteRatingIn):
    """GET response. Server owns updated_at."""
    updated_at: str


class OsmRouteOut(BaseModel):
    """GET /api/osm-routes/at: one Waymarked Trails route at the clicked spot."""

    site: str
    # The OSM relation id (digits only).
    id: str
    name: str
    ref: str
    # Network level: INT, NAT, REG or LOC.
    group: str
    itinerary: list[str]
    # For https://<site>.waymarkedtrails.org/api/v1/symbols/id/<symbol_id>.svg
    symbol_id: str | None
    # Describes the waymark; the only label of many unnamed local routes.
    symbol_description: str
    url: str
    # True when the user already imported it (route web-waymarked-<site>-<id>).
    adopted: bool


class OsmRoutesAtResponse(BaseModel):
    """GET /api/osm-routes/at: the routes found, and the sites that failed."""

    results: list[OsmRouteOut]
    # site -> user-facing message, for the sites whose lookup failed.
    errors: dict[str, str] = {}


class OsmRouteDetailsResponse(BaseModel):
    """GET /api/osm-routes/{site}/{id}: one route's details and geometry."""

    site: str
    id: str
    name: str
    ref: str
    length_km: float
    operator: str
    website: str
    description: str
    url: str
    geometry: MultiLineStringGeometry


# ── Server self-update (see `updater`) ──

UpdateState = Literal["idle", "checking", "updating", "restarting", "failed"]


class UpdateCommit(BaseModel):
    sha: str
    subject: str


class UpdateStatus(BaseModel):
    """GET /api/admin/update: the server's self-update."""

    # False when the server has no `update_dir` (then nothing else is set).
    enabled: bool
    branch: str | None = None
    # The commit (short sha) the server runs; None if it doesn't run a release.
    running: str | None = None
    state: UpdateState = "idle"
    # What a check or an update is doing right now.
    step: str | None = None
    # The outcome of the last check or update, when it needs saying.
    message: str | None = None
    error: str | None = None
    # The branch's newest commit as of `checked_at`, and the commits it adds
    # to the running one (newest first).
    latest: str | None = None
    commits: list[UpdateCommit] = []
    checked_at: str | None = None
    finished_at: str | None = None


class UpdateRequest(BaseModel):
    """POST /api/admin/update: the admin's password, and the commit they
    were shown (nothing is deployed if the branch has moved on since)."""

    password: str = Field(max_length=1024)
    commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{7,40}$")
