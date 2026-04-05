from enum import Enum

from pydantic import BaseModel, field_validator


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

    @field_validator("sport", mode="before")
    @classmethod
    def normalize_sport(cls, v: str | list[str]) -> list[str]:
        if isinstance(v, str):
            return [v]
        return v


class WishmapConfig(BaseModel):
    title: str = "wishmap"
    pins: list[PinConfig] = []
    routes: list[RouteConfig] = []
