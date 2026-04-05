from pathlib import Path
from typing import Any

from wishmap.gpx import parse_gpx
from wishmap.models import PinConfig, RouteConfig


def pins_to_geojson(pins: list[PinConfig]) -> dict[str, Any]:
    """Convert pins to a GeoJSON FeatureCollection of Points."""
    features = []
    for pin in pins:
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [pin.lon, pin.lat],
            },
            "properties": {
                "id": pin.id,
                "name": pin.name,
                "kind": "pin",
                "sport": pin.sport,
                "status": pin.status.value,
                "tags": pin.tags,
                "notes": pin.notes,
            },
        })
    return {"type": "FeatureCollection", "features": features}


def routes_to_geojson(
    routes: list[RouteConfig], base_path: Path
) -> dict[str, Any]:
    """Convert routes to a GeoJSON FeatureCollection of LineStrings."""
    features = []
    for route in routes:
        gpx_path = base_path / route.gpx
        coords = parse_gpx(gpx_path)
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                "coordinates": coords,
            },
            "properties": {
                "id": route.id,
                "name": route.name,
                "kind": "route",
                "sport": route.sport,
                "status": route.status.value,
                "tags": route.tags,
                "notes": route.notes,
                "color": route.color,
            },
        })
    return {"type": "FeatureCollection", "features": features}


def route_start_pins_to_geojson(
    routes: list[RouteConfig], base_path: Path
) -> list[dict[str, Any]]:
    """Generate a Point feature for the start of each route's GPX."""
    features: list[dict[str, Any]] = []
    for route in routes:
        gpx_path = base_path / route.gpx
        coords = parse_gpx(gpx_path)
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": coords[0],
            },
            "properties": {
                "id": f"{route.id}-start",
                "name": route.name,
                "kind": "route_start",
                "sport": route.sport,
                "status": route.status.value,
                "tags": route.tags,
                "notes": route.notes,
                "color": route.color,
            },
        })
    return features
