from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator


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
    color: str = "#3388ff"
    gpx: str
    strava_id: int | None = None

    @field_validator("sport", mode="before")
    @classmethod
    def normalize_sport(cls, v: str | list[str]) -> list[str]:
        if isinstance(v, str):
            return [v]
        return v


class GarminConfig(BaseModel):
    username: str
    password: str = ""
    password_file: str = ""
    password_pass: str = ""
    activity_limit: int = 1000
    gpx_dir: str = "data/garmin"
    tokenstore: str = "~/.garminconnect"
    sport_filter: list[str] = []


class StravaConfig(BaseModel):
    client_id: str
    client_secret: str = ""
    client_secret_file: str = ""
    client_secret_pass: str = ""
    activity_limit: int = 1000
    gpx_dir: str = "data/strava"
    tokenstore: str = "~/.wishmap/strava_tokens.json"
    sport_filter: list[str] = []


class WishmapConfig(BaseModel):
    title: str = "wishmap"
    includes: list[str] = []
    # Optional pass entry to use as the smoke-test target when warming
    # gpg-agent via POST /api/unlock. If unset, the union of
    # garmin.password_pass and strava.client_secret_pass is used.
    # Set this only if you want unlock to verify against a specific
    # entry (e.g. one encrypted to a fast subkey).
    warm_pass_entry: str = ""
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
    color: str | None = None
    strava_id: int | None = None


class PointGeometry(BaseModel):
    type: Literal["Point"] = "Point"
    coordinates: list[float]


class LineStringGeometry(BaseModel):
    type: Literal["LineString"] = "LineString"
    coordinates: list[list[float]]


class Feature(BaseModel):
    type: Literal["Feature"] = "Feature"
    geometry: PointGeometry | LineStringGeometry
    properties: FeatureProperties


class FeatureCollection(BaseModel):
    type: Literal["FeatureCollection"] = "FeatureCollection"
    features: list[Feature]


class ConfigResponse(BaseModel):
    title: str


class RouteRatingIn(BaseModel):
    """PUT body. Any subset of axes may be sent.

    Omitted axes preserve their stored value; explicit None clears that axis.
    """
    fun: int | None = Field(None, ge=1, le=5)
    difficulty: int | None = Field(None, ge=1, le=5)
    scenery: int | None = Field(None, ge=1, le=5)


class RouteRating(RouteRatingIn):
    """GET response. Server owns updated_at."""
    updated_at: str
