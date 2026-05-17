from pathlib import Path

from wishmap import ratings

USER = 1
OTHER = 2


def test_get_all_empty(db_path: Path) -> None:
    assert ratings.get_all(db_path, USER) == {}


def test_get_all_returns_rows(db_path: Path) -> None:
    ratings.upsert(db_path, USER, "r1", {"fun": 4, "difficulty": 3, "scenery": 5})
    ratings.upsert(db_path, USER, "r2", {"fun": 2})

    all_ratings = ratings.get_all(db_path, USER)
    assert set(all_ratings.keys()) == {"r1", "r2"}
    assert all_ratings["r1"]["fun"] == 4
    assert all_ratings["r1"]["difficulty"] == 3
    assert all_ratings["r1"]["scenery"] == 5
    assert all_ratings["r2"] == {
        "fun": 2,
        "difficulty": None,
        "scenery": None,
        "updated_at": all_ratings["r2"]["updated_at"],
    }


def test_ratings_are_scoped_per_user(db_path: Path) -> None:
    # The same route id rated by two users: two independent rows.
    ratings.upsert(db_path, USER, "r1", {"fun": 4})
    ratings.upsert(db_path, OTHER, "r1", {"fun": 1, "scenery": 2})

    assert ratings.get_all(db_path, USER)["r1"]["fun"] == 4
    assert ratings.get_all(db_path, USER)["r1"]["scenery"] is None
    assert ratings.get_all(db_path, OTHER)["r1"]["fun"] == 1
    assert ratings.get_all(db_path, 3) == {}


def test_upsert_merges_only_own_row(db_path: Path) -> None:
    # A partial patch merges into this user's row, never another user's.
    ratings.upsert(db_path, OTHER, "r1", {"fun": 1, "difficulty": 1, "scenery": 1})
    out = ratings.upsert(db_path, USER, "r1", {"fun": 5})
    assert out["difficulty"] is None
    assert out["scenery"] is None


def test_upsert_inserts_new_row(db_path: Path) -> None:
    out = ratings.upsert(db_path, USER, "r1", {"fun": 4})
    assert out["fun"] == 4
    assert out["difficulty"] is None
    assert out["scenery"] is None
    assert out["updated_at"]


def test_upsert_updates_existing_row(db_path: Path) -> None:
    ratings.upsert(db_path, USER, "r1", {"fun": 4, "difficulty": 3, "scenery": 5})
    out = ratings.upsert(db_path, USER, "r1", {"fun": 2})
    assert out["fun"] == 2
    assert out["difficulty"] == 3
    assert out["scenery"] == 5


def test_upsert_null_clears_one_axis(db_path: Path) -> None:
    ratings.upsert(db_path, USER, "r1", {"fun": 4, "difficulty": 3, "scenery": 5})
    out = ratings.upsert(db_path, USER, "r1", {"fun": None})
    assert out["fun"] is None
    assert out["difficulty"] == 3
    assert out["scenery"] == 5


def test_upsert_absent_axis_preserved(db_path: Path) -> None:
    ratings.upsert(db_path, USER, "r1", {"fun": 4, "difficulty": 3, "scenery": 5})
    out = ratings.upsert(db_path, USER, "r1", {"difficulty": 1})
    assert out["fun"] == 4
    assert out["difficulty"] == 1
    assert out["scenery"] == 5


def test_upsert_empty_patch_refreshes_updated_at(db_path: Path) -> None:
    first = ratings.upsert(db_path, USER, "r1", {"fun": 4})
    second = ratings.upsert(db_path, USER, "r1", {})
    assert second["fun"] == 4
    assert second["difficulty"] is None
    assert second["scenery"] is None
    assert second["updated_at"] >= first["updated_at"]


def test_upsert_return_matches_stored_row(db_path: Path) -> None:
    out = ratings.upsert(db_path, USER, "r1", {"fun": 3, "scenery": 4})
    assert ratings.get_all(db_path, USER)["r1"] == out
