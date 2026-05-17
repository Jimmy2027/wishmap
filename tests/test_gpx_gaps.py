"""Route display geometry: tracks that jump (a gap in the recording) are
split, not drawn across, and every track is simplified and rounded."""

from wishmap.geojson import route_features
from wishmap.gpx import COORD_DECIMALS, simplify_line, split_at_gaps, track_parts
from wishmap.models import RouteConfig

# ~100 m zig-zag steps (not collinear, so simplification keeps every point),
# then a ~130 km jump (Domodossola -> Milan), then ~100 m again.
NEAR_A = [[8.290, 46.120], [8.291, 46.121], [8.292, 46.120]]
FAR_B = [[9.190, 45.460], [9.191, 45.460]]


def _route() -> RouteConfig:
    return RouteConfig(
        id="r1", name="Jumpy", sport=["biking"], status="done", gpx="r1.gpx"  # type: ignore[arg-type]
    )


def test_split_at_gaps_keeps_continuous_track_whole() -> None:
    assert split_at_gaps(NEAR_A) == [NEAR_A]


def test_split_at_gaps_breaks_at_jump() -> None:
    assert split_at_gaps(NEAR_A + FAR_B) == [NEAR_A, FAR_B]


def test_split_at_gaps_empty_and_custom_threshold() -> None:
    assert split_at_gaps([]) == [[]]
    # ~135 m between the points: a 50 m threshold splits every step.
    assert split_at_gaps(NEAR_A, max_gap_m=50) == [[p] for p in NEAR_A]


def test_route_feature_is_linestring_without_gap() -> None:
    line, start = route_features(_route(), NEAR_A)
    assert line.geometry.type == "LineString"
    assert line.geometry.coordinates == NEAR_A
    assert start.geometry.coordinates == NEAR_A[0]


def test_route_feature_is_multilinestring_across_gap() -> None:
    line, start = route_features(_route(), NEAR_A + FAR_B)
    assert line.geometry.type == "MultiLineString"
    assert line.geometry.coordinates == [NEAR_A, FAR_B]
    assert start.geometry.coordinates == NEAR_A[0]


def test_simplify_drops_collinear_points_and_keeps_endpoints() -> None:
    straight = [[8.0 + i * 1e-4, 46.0] for i in range(50)]
    assert simplify_line(straight, 1e-5) == [straight[0], straight[-1]]
    assert simplify_line(NEAR_A, 1e-5) == NEAR_A
    assert simplify_line(NEAR_A[:2], 1e-5) == NEAR_A[:2]


def test_simplify_handles_long_tracks_without_recursion() -> None:
    # Deeper than CPython's recursion limit if Douglas-Peucker recursed.
    zigzag = [[8.0 + i * 1e-4, 46.0 + (i % 2) * 1e-3] for i in range(30_000)]
    assert simplify_line(zigzag, 1e-5) == zigzag


def test_track_parts_simplifies_each_part_and_rounds() -> None:
    noisy = [[8.290000001, 46.120000001], [8.2905, 46.12], [8.291, 46.12]]
    parts = track_parts(noisy + FAR_B)
    assert parts == [[[8.29, 46.12], [8.291, 46.12]], FAR_B]
    for part in parts:
        for lon, lat in part:
            assert round(lon, COORD_DECIMALS) == lon
            assert round(lat, COORD_DECIMALS) == lat
