"""Shared fixtures.

HTTP tests run the real app over a tmp_path install: `wishmap_toml` (a
module may override it) is written to `install/wishmap.toml` and
WISHMAP_CONFIG points at it. `client` is an anonymous TestClient whose
lifespan sets up a fresh `app.state.ctx`; `user_client` and
`second_user_client` are further cookie jars on that same running app,
signed in as two different freshly registered users (alice and bob).
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.helpers import signed_in_client
from wishmap import users, vault
from wishmap.app import app


@pytest.fixture(autouse=True)
def fast_password_hashing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cheap scrypt for tests (hashes and wrapped vault keys still carry
    and honor their own cost)."""
    monkeypatch.setattr(users, "SCRYPT_N", 2**4)
    monkeypatch.setattr(users, "_dummy_hash", None)
    monkeypatch.setattr(vault, "SCRYPT_N", 2**4)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "wishmap.db"


@pytest.fixture
def wishmap_toml() -> str:
    return 'title = "test"\n'


@pytest.fixture
def install(
    tmp_path: Path, wishmap_toml: str, monkeypatch: pytest.MonkeyPatch
) -> Path:
    (tmp_path / "wishmap.toml").write_text(wishmap_toml)
    monkeypatch.setenv("WISHMAP_CONFIG", str(tmp_path / "wishmap.toml"))
    return tmp_path


@pytest.fixture
def client(install: Path) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


@pytest.fixture
def user_client(client: TestClient) -> Iterator[TestClient]:
    c = signed_in_client("alice")
    yield c
    c.close()


@pytest.fixture
def second_user_client(client: TestClient) -> Iterator[TestClient]:
    c = signed_in_client("bob")
    yield c
    c.close()
