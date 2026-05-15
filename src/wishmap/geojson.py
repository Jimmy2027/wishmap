from pathlib import Path

from wishmap.gpx import parse_gpx
from wishmap.models import (
    Feature,
    FeatureCollection,
    FeatureProperties,
    LineStringGeometry,
    PinConfig,
    PointGeometry,
    RouteConfig,
)


def pins_to_geojson(pins: list[PinConfig]) -> FeatureCollection:
    """Convert pins to a GeoJSON FeatureCollection of Points."""
    features = []
    for pin in pins:
        features.append(Feature(
            geometry=PointGeometry(coordinates=[pin.lon, pin.lat]),
            properties=FeatureProperties(
                id=pin.id,
                name=pin.name,
                kind="pin",
                sport=pin.sport,
                status=pin.status.value,
                tags=pin.tags,
                notes=pin.notes,
            ),
        ))
    return FeatureCollection(features=features)


def routes_to_geojson(
    routes: list[RouteConfig], base_path: Path
) -> FeatureCollection:
    """Convert routes to a GeoJSON FeatureCollection of LineStrings."""
    features = []
    for route in routes:
        gpx_path = base_path / route.gpx
        coords = parse_gpx(gpx_path)
        features.append(Feature(
            geometry=LineStringGeometry(coordinates=coords),
            properties=FeatureProperties(
                id=route.id,
                name=route.name,
                kind="route",
                sport=route.sport,
                status=route.status.value,
                tags=route.tags,
                notes=route.notes,
                color=route.color,
                strava_id=route.strava_id,
            ),
        ))
    return FeatureCollection(features=features)


def route_start_pins_to_geojson(
    routes: list[RouteConfig], base_path: Path
) -> list[Feature]:
    """Generate a Point feature for the start of each route's GPX."""
    features: list[Feature] = []
    for route in routes:
        gpx_path = base_path / route.gpx
        coords = parse_gpx(gpx_path)
        features.append(Feature(
            geometry=PointGeometry(coordinates=coords[0]),
            properties=FeatureProperties(
                id=f"{route.id}-start",
                name=route.name,
                kind="route_start",
                sport=route.sport,
                status=route.status.value,
                tags=route.tags,
                notes=route.notes,
                color=route.color,
                strava_id=route.strava_id,
            ),
        ))
    return features
