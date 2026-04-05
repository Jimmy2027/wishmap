from pathlib import Path

import gpxpy


def parse_gpx(path: Path) -> list[list[float]]:
    """Parse a GPX file and return coordinates in GeoJSON order [lon, lat]."""
    with open(path) as f:
        gpx = gpxpy.parse(f)

    coords: list[list[float]] = []
    for track in gpx.tracks:
        for segment in track.segments:
            for point in segment.points:
                coords.append([point.longitude, point.latitude])

    if not coords:
        raise ValueError(f"No track points found in GPX file: {path}")

    return coords
