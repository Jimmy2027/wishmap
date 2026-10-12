import math
from pathlib import Path
from typing import IO

import gpxpy


def parse_gpx_data(source: str | IO[str], label: str = "GPX data") -> list[list[float]]:
    """Parse GPX text (or a text stream) into [lon, lat] coordinate pairs.

    Used both for on-disk files (via `parse_gpx`) and for validating an
    uploaded GPX in memory before it is written to disk. Raises
    `ValueError` when no track points are present so callers can reject an
    empty/garbage upload before anything lands on disk or in the DB.
    """
    gpx = gpxpy.parse(source)

    coords: list[list[float]] = []
    for track in gpx.tracks:
        for segment in track.segments:
            for point in segment.points:
                coords.append([point.longitude, point.latitude])

    if not coords:
        raise ValueError(f"No track points found in {label}")

    return coords


def parse_gpx(path: Path) -> list[list[float]]:
    """Parse a GPX file and return coordinates in GeoJSON order [lon, lat]."""
    with open(path) as f:
        return parse_gpx_data(f, str(path))


# Consecutive track points further apart than this are treated as a gap in
# the recording (GPS loss, a watch left running on a train or in a car), not
# as movement: the line is broken there instead of drawn straight across.
MAX_GAP_M = 2000.0

_EARTH_RADIUS_M = 6_371_000.0


def _distance_m(a: list[float], b: list[float]) -> float:
    """Great-circle (haversine) distance in metres between two [lon, lat] points."""
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(min(1.0, h)))


def length_m(coords: list[list[float]]) -> float:
    """Length in metres of the line through [lon, lat] `coords`."""
    return sum(_distance_m(a, b) for a, b in zip(coords, coords[1:]))


def split_at_gaps(
    coords: list[list[float]], max_gap_m: float = MAX_GAP_M
) -> list[list[list[float]]]:
    """Split [lon, lat] coordinates into parts wherever two consecutive points
    are more than `max_gap_m` apart. Always returns at least one part.
    """
    parts: list[list[list[float]]] = [[]]
    for point in coords:
        current = parts[-1]
        if current and _distance_m(current[-1], point) > max_gap_m:
            current = []
            parts.append(current)
        current.append(point)
    return parts


# ── Display geometry ──
# Real GPX tracks are wildly over-sampled for a map (~1.4k points per route,
# 1.66M over a 1.2k-route account): sent raw, /api/routes is ~63 MB of JSON
# the browser must download, parse and draw. Every route is therefore thinned
# once, when its GPX is parsed: split at recording gaps, Douglas-Peucker
# simplified, coordinates rounded. Lossy, but invisible on the map.

# Douglas-Peucker tolerance in degrees. 1e-5 deg ≈ 1.1 m at temperate
# latitudes; at the map's deepest zoom (19, ~0.3 m/px) that is ~3 px —
# invisible on a trail polyline — yet it keeps only ~30% of the points.
# 2e-5 ≈ 2 m and 5e-5 ≈ 5.5 m shrink further at the cost of visible
# smoothing on tight switchbacks when fully zoomed in.
SIMPLIFY_EPS_DEG = 1e-5

# Coordinate precision. 5 decimals ≈ 1.1 m, matched to the tolerance;
# trimming the float tails is most of the remaining size win.
COORD_DECIMALS = 5

# Bump whenever `track_parts` output changes (tolerance, rounding, gap
# threshold) so cached geometry built the old way is rebuilt.
TRACK_VERSION = 1


def simplify_line(coords: list[list[float]], eps: float) -> list[list[float]]:
    """Douglas-Peucker line simplification (iterative, `[lon, lat]` points).

    Iterative (explicit stack) rather than recursive on purpose: a single GPX
    track reaches 21k points, which overflows CPython's recursion limit.
    Distance is planar in degree space and compared squared (no per-point
    sqrt) — good enough for a display-only tolerance. Both endpoints are
    always kept.
    """
    n = len(coords)
    if n < 3:
        return coords
    keep = [False] * n
    keep[0] = keep[-1] = True
    eps2 = eps * eps
    stack = [(0, n - 1)]
    while stack:
        start, end = stack.pop()
        x1, y1 = coords[start][0], coords[start][1]
        x2, y2 = coords[end][0], coords[end][1]
        dx, dy = x2 - x1, y2 - y1
        seg2 = dx * dx + dy * dy
        dmax2 = 0.0
        idx = -1
        for i in range(start + 1, end):
            x, y = coords[i][0], coords[i][1]
            if seg2 == 0.0:
                px, py = x1, y1
            else:
                t = ((x - x1) * dx + (y - y1) * dy) / seg2
                t = 0.0 if t < 0.0 else 1.0 if t > 1.0 else t
                px, py = x1 + t * dx, y1 + t * dy
            ddx, ddy = x - px, y - py
            d2 = ddx * ddx + ddy * ddy
            if d2 > dmax2:
                dmax2 = d2
                idx = i
        if dmax2 > eps2 and idx != -1:
            keep[idx] = True
            stack.append((start, idx))
            stack.append((idx, end))
    return [coords[i] for i in range(n) if keep[i]]


def track_parts(coords: list[list[float]]) -> list[list[list[float]]]:
    """Turn a parsed track into its display geometry: one part per stretch
    between recording gaps (see `split_at_gaps`), each simplified and rounded.

    The first point of the first part is the track's (rounded) start.
    """
    return [
        [
            [round(lon, COORD_DECIMALS), round(lat, COORD_DECIMALS)]
            for lon, lat in simplify_line(part, SIMPLIFY_EPS_DEG)
        ]
        for part in split_at_gaps(coords)
    ]
