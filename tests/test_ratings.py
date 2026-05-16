import sqlite3
from pathlib import Path

import pytest

from wishmap import ratings


def test_connect_creates_schema(db_path: Path) -> None:
    with ratings._connect(db_path):
        pass

    assert db_path.is_file()
    conn = sqlite3.connect(db_path)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(route_ratings)")}
    finally:
        conn.close()
    assert cols == {"route_id", "fun", "difficulty", "scenery", "updated_at"}


def test_connect_commits_on_success(db_path: Path) -> None:
    with ratings._connect(db_path) as conn:
        conn.execute(
            "INSERT INTO route_ratings VALUES ('r1', 4, 3, 5, 'now')"
        )

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT route_id, fun FROM route_ratings"
        ).fetchone()
    finally:
        conn.close()
    assert row == ("r1", 4)


def test_connect_rolls_back_on_exception(db_path: Path) -> None:
    # Seed a row, then attempt a doomed transaction.
    with ratings._connect(db_path) as conn:
        conn.execute(
            "INSERT INTO route_ratings VALUES ('r1', 1, 1, 1, 'seed')"
        )

    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        with ratings._connect(db_path) as conn:
            conn.execute(
                "INSERT INTO route_ratings VALUES ('r2', 2, 2, 2, 'pending')"
            )
            raise Boom

    assert ratings.get_all(db_path) == {
        "r1": {"fun": 1, "difficulty": 1, "scenery": 1, "updated_at": "seed"}
    }


def test_get_all_empty(db_path: Path) -> None:
    assert ratings.get_all(db_path) == {}


def test_get_all_returns_rows(db_path: Path) -> None:
    ratings.upsert(db_path, "r1", {"fun": 4, "difficulty": 3, "scenery": 5})
    ratings.upsert(db_path, "r2", {"fun": 2})

    all_ratings = ratings.get_all(db_path)
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


def test_upsert_inserts_new_row(db_path: Path) -> None:
    out = ratings.upsert(db_path, "r1", {"fun": 4})
    assert out["fun"] == 4
    assert out["difficulty"] is None
    assert out["scenery"] is None
    assert out["updated_at"]


def test_upsert_updates_existing_row(db_path: Path) -> None:
    ratings.upsert(db_path, "r1", {"fun": 4, "difficulty": 3, "scenery": 5})
    out = ratings.upsert(db_path, "r1", {"fun": 2})
    assert out["fun"] == 2
    assert out["difficulty"] == 3
    assert out["scenery"] == 5


def test_upsert_null_clears_one_axis(db_path: Path) -> None:
    ratings.upsert(db_path, "r1", {"fun": 4, "difficulty": 3, "scenery": 5})
    out = ratings.upsert(db_path, "r1", {"fun": None})
    assert out["fun"] is None
    assert out["difficulty"] == 3
    assert out["scenery"] == 5


def test_upsert_absent_axis_preserved(db_path: Path) -> None:
    ratings.upsert(db_path, "r1", {"fun": 4, "difficulty": 3, "scenery": 5})
    out = ratings.upsert(db_path, "r1", {"difficulty": 1})
    assert out["fun"] == 4
    assert out["difficulty"] == 1
    assert out["scenery"] == 5


def test_upsert_empty_patch_refreshes_updated_at(db_path: Path) -> None:
    first = ratings.upsert(db_path, "r1", {"fun": 4})
    second = ratings.upsert(db_path, "r1", {})
    assert second["fun"] == 4
    assert second["difficulty"] is None
    assert second["scenery"] is None
    assert second["updated_at"] >= first["updated_at"]
