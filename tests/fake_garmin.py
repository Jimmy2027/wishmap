"""A fake `garminconnect.Garmin` for the Garmin sync tests (no network).

Mirrors the parts of garminconnect 0.3's API wishmap uses, as read from
the installed package: `Garmin(email, password, return_on_mfa=…)`,
`login(tokenstore)` — where a tokenstore string longer than 512 characters
is token data and anything shorter a directory path —, `resume_login`,
`client.dumps()`, `get_activities`, `download_activity`.

State lives on the class (reset by `FakeGarmin.reset()`): the valid Garmin
accounts, whether a credential login wants an MFA code, the tokens Garmin
currently accepts, and the activities the account has.
"""

import itertools
import json
import uuid
from typing import Any, ClassVar

from garminconnect import GarminConnectAuthenticationError

GARMIN_USER = "me@example.com"
GARMIN_PASSWORD = "garmin-secret-password"
GOOD_CODE = "123456"


def gpx_bytes(lat: float, lon: float) -> bytes:
    return f"""<?xml version="1.0"?>
<gpx version="1.1" creator="fake garmin" xmlns="http://www.topografix.com/GPX/1/1">
  <trk><name>t</name><trkseg>
    <trkpt lat="{lat}" lon="{lon}"/>
    <trkpt lat="{lat + 0.01}" lon="{lon + 0.01}"/>
  </trkseg></trk>
</gpx>
""".encode()


def activity(
    activity_id: int,
    name: str = "Garmin hike",
    type_key: str = "hiking",
    start_gmt: str = "2026-06-01 08:00:00",
    distance: float = 12000.0,
) -> dict[str, Any]:
    return {
        "activityId": activity_id,
        "activityName": name,
        "activityType": {"typeKey": type_key},
        "startTimeLocal": start_gmt,
        "startTimeGMT": start_gmt,
        "distance": distance,
        "duration": 5400.0,
        "startLatitude": 46.0 + activity_id / 1000,
        "startLongitude": 9.0,
    }


class _FakeInner:
    """Stands in for garminconnect's `Client` (the token holder)."""

    def __init__(self) -> None:
        self.di_token: str | None = None

    def dumps(self) -> str:
        return json.dumps(
            {
                "di_token": self.di_token,
                "di_refresh_token": None if self.di_token is None else f"refresh-{self.di_token}",
                "di_client_id": "fake-client",
            }
        )


class FakeGarmin:
    accounts: ClassVar[dict[str, str]] = {}
    needs_mfa = False
    # Token values Garmin currently accepts.
    valid_tokens: ClassVar[set[str]] = set()
    # Issue a new token on every token login (as a refresh near expiry does).
    refresh_on_token_login = False
    activities: ClassVar[list[dict[str, Any]]] = []
    instances: ClassVar[list["FakeGarmin"]] = []
    _counter = itertools.count(1)

    class ActivityDownloadFormat:
        GPX = "gpx"

    @classmethod
    def reset(cls) -> None:
        cls.accounts = {GARMIN_USER: GARMIN_PASSWORD}
        cls.needs_mfa = False
        cls.valid_tokens = set()
        cls.refresh_on_token_login = False
        cls.activities = []
        cls.instances = []

    @classmethod
    def credential_logins(cls) -> list["FakeGarmin"]:
        return [c for c in cls.instances if c.username is not None]

    def __init__(
        self,
        email: str | None = None,
        password: str | None = None,
        is_cn: bool = False,
        prompt_mfa: Any = None,
        return_on_mfa: bool = False,
    ) -> None:
        self.username = email
        self.password = password
        self.return_on_mfa = return_on_mfa
        self.client = _FakeInner()
        self.login_args: list[str | None] = []
        self.downloads: list[str] = []
        FakeGarmin.instances.append(self)

    def _issue_token(self) -> None:
        # Unique and distinctive, so tests can search files and logs for it.
        token = f"fake-di-token-{next(FakeGarmin._counter)}-{uuid.uuid4().hex}"
        FakeGarmin.valid_tokens.add(token)
        self.client.di_token = token

    def login(self, tokenstore: str | None = None) -> tuple[str | None, None]:
        self.login_args.append(tokenstore)
        if tokenstore:
            # garminconnect reads a string of up to 512 characters as a path.
            assert len(tokenstore) > 512, "token data must be padded past 512 chars"
            data = json.loads(tokenstore)
            if data.get("di_token") not in FakeGarmin.valid_tokens:
                raise GarminConnectAuthenticationError("Failed to retrieve social profile")
            self.client.di_token = data["di_token"]
            if FakeGarmin.refresh_on_token_login:
                FakeGarmin.valid_tokens.discard(data["di_token"])
                self._issue_token()
            return None, None
        if not self.username or not self.password:
            raise GarminConnectAuthenticationError("Username and password are required")
        if FakeGarmin.accounts.get(self.username) != self.password:
            raise GarminConnectAuthenticationError("Authentication failed (401 Unauthorized)")
        if FakeGarmin.needs_mfa:
            assert self.return_on_mfa, "wishmap must never let garminconnect prompt"
            return "needs_mfa", None
        self._issue_token()
        return None, None

    def resume_login(self, client_state: dict[str, Any], code: str) -> tuple[None, None]:
        if code != GOOD_CODE:
            raise GarminConnectAuthenticationError("MFA failed: Invalid code")
        self._issue_token()
        return None, None

    def get_activities(self, start: int, limit: int) -> list[dict[str, Any]]:
        assert self.client.di_token in FakeGarmin.valid_tokens
        return [dict(a) for a in FakeGarmin.activities[start : start + limit]]

    def download_activity(self, activity_id: str, dl_fmt: Any = None) -> bytes:
        assert self.client.di_token in FakeGarmin.valid_tokens
        self.downloads.append(activity_id)
        return gpx_bytes(46.0 + int(activity_id) / 1000, 9.0)
