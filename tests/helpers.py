"""Helpers shared by the HTTP tests (fixtures live in conftest.py)."""

from fastapi.testclient import TestClient

from wishmap import users
from wishmap.app import app
from wishmap.state import AppContext, UserState

PASSWORD = "correct horse battery"

MINIMAL_GPX = """<?xml version="1.0"?>
<gpx version="1.1" creator="test" xmlns="http://www.topografix.com/GPX/1/1">
  <trk><name>t</name><trkseg>
    <trkpt lat="46.9" lon="9.7"/>
    <trkpt lat="46.91" lon="9.71"/>
  </trkseg></trk>
</gpx>
"""


def ctx() -> AppContext:
    """The running app's context (set by the `client` fixture's lifespan)."""
    return app.state.ctx


def signed_in_client(username: str, password: str = PASSWORD) -> TestClient:
    """A new cookie jar on the running app, registered and signed in.

    Not entered as a context manager: that would run a second lifespan
    and replace the app context the `client` fixture set up.
    """
    c = TestClient(app)
    resp = c.post(
        "/api/auth/register", json={"username": username, "password": password}
    )
    assert resp.status_code == 201, resp.text
    return c


def user_id(username: str) -> int:
    user = users.get_by_username(ctx().db_path, username)
    assert user is not None
    return user.id


def user_state(username: str) -> UserState:
    return ctx().registry.get(user_id(username))
