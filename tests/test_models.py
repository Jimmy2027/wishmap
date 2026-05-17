import pytest
from pydantic import ValidationError

from wishmap.models import RouteRating, RouteRatingIn, WishmapConfig


@pytest.mark.parametrize("value", [1, 2, 3, 4, 5])
def test_rating_in_accepts_valid_values(value: int) -> None:
    r = RouteRatingIn(fun=value, difficulty=value, scenery=value)
    assert r.fun == value
    assert r.difficulty == value
    assert r.scenery == value


@pytest.mark.parametrize("value", [0, 6, -1, 100])
def test_rating_in_rejects_out_of_range(value: int) -> None:
    with pytest.raises(ValidationError):
        RouteRatingIn(fun=value)


def test_rating_in_rejects_non_int() -> None:
    with pytest.raises(ValidationError):
        RouteRatingIn.model_validate({"fun": "great"})


def test_rating_in_explicit_none_preserved() -> None:
    r = RouteRatingIn(fun=None, difficulty=3, scenery=None)
    assert r.fun is None
    assert r.difficulty == 3
    assert r.scenery is None


def test_rating_in_exclude_unset_round_trip() -> None:
    """Omitted axis ≠ explicit null in the dumped patch — this is the
    partial-update contract the backend relies on."""
    r = RouteRatingIn.model_validate({"fun": 4})
    assert r.model_dump(exclude_unset=True) == {"fun": 4}

    r = RouteRatingIn.model_validate({"fun": None})
    assert r.model_dump(exclude_unset=True) == {"fun": None}

    r = RouteRatingIn.model_validate({})
    assert r.model_dump(exclude_unset=True) == {}


def test_rating_out_requires_updated_at() -> None:
    with pytest.raises(ValidationError):
        RouteRating.model_validate({"fun": 4})

    r = RouteRating(fun=4, difficulty=None, scenery=None, updated_at="2026-05-15T00:00:00+00:00")
    assert r.updated_at == "2026-05-15T00:00:00+00:00"


def test_config_ignores_legacy_pass_keys() -> None:
    """Configs written for the removed GPG/pass flow still load: the
    `*_pass` / `warm_pass_entry` keys are ignored, not rejected."""
    cfg = WishmapConfig.model_validate(
        {
            "warm_pass_entry": "wishmap/smoke",
            "garmin": {"username": "u", "password_pass": "Garmin"},
            "strava": {"client_id": "1", "client_secret_pass": "Strava"},
        }
    )
    assert cfg.garmin is not None and cfg.strava is not None
    assert not hasattr(cfg, "warm_pass_entry")
    assert not hasattr(cfg.garmin, "password_pass")
    assert not hasattr(cfg.strava, "client_secret_pass")
