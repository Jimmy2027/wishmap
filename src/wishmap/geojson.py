from wishmap.gpx import track_parts
from wishmap.models import (
    Feature,
    FeatureCollection,
    FeatureProperties,
    LineStringGeometry,
    MultiLineStringGeometry,
    PinConfig,
    PointGeometry,
    RouteConfig,
)


def pin_to_feature(pin: PinConfig) -> Feature:
    """Convert one pin to a GeoJSON Point feature."""
    return Feature(
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
    )


def pins_to_geojson(pins: list[PinConfig]) -> FeatureCollection:
    """Convert pins to a GeoJSON FeatureCollection of Points."""
    return FeatureCollection(features=[pin_to_feature(pin) for pin in pins])


def route_properties(route: RouteConfig) -> FeatureProperties:
    """The properties of a route's LineString feature.

    Its route-start Point carries the same, with id `<route id>-start` and
    kind `route_start` (see `start_properties`).
    """
    return FeatureProperties(
        id=route.id,
        name=route.name,
        kind="route",
        sport=route.sport,
        status=route.status.value,
        tags=route.tags,
        notes=route.notes,
        strava_id=route.strava_id,
    )


def start_properties(route_props: FeatureProperties) -> FeatureProperties:
    """The route-start Point's properties, derived from the route's."""
    return route_props.model_copy(
        update={"id": f"{route_props.id}-start", "kind": "route_start"}
    )


def route_features(
    route: RouteConfig, coords: list[list[float]]
) -> tuple[Feature, Feature]:
    """Build a route's line and route-start Point from parsed coordinates.

    The coordinates are thinned to display geometry first (see
    `track_parts`); a caller that already has the parts (the cached map
    build) uses `route_features_from_parts`.
    """
    return route_features_from_parts(route, track_parts(coords))


def route_features_from_parts(
    route: RouteConfig, parts: list[list[list[float]]]
) -> tuple[Feature, Feature]:
    """Build a route's line and route-start Point from its display geometry.

    Takes the geometry rather than a path so a caller that needs both
    features (every map build) parses each GPX once, not once per feature.

    A track that jumps (`track_parts` split it at a recording gap) becomes
    a MultiLineString so the map doesn't draw a straight line across the
    gap; any other track stays a plain LineString.
    """
    props = route_properties(route)
    geometry = (
        LineStringGeometry(coordinates=parts[0])
        if len(parts) == 1
        else MultiLineStringGeometry(coordinates=parts)
    )
    line = Feature(geometry=geometry, properties=props)
    start = Feature(
        geometry=PointGeometry(coordinates=parts[0][0]),
        properties=start_properties(props),
    )
    return line, start
