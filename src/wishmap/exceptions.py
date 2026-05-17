"""Domain exceptions for wishmap sync operations."""


class SyncError(Exception):
    """Raised when a sync operation cannot complete.

    Carries a user-readable message (rate-limit guidance, auth-failed
    instructions, etc.) suitable for surfacing to the web UI or the CLI.
    """


class MfaRequiredError(SyncError):
    """Garmin's credential login is waiting for a one-time MFA code.

    The half-finished login is kept in memory by `garmin`; surfaces to the
    web UI as `needs_mfa: true` in the sync status so the frontend can
    prompt for the code and POST it to /api/garmin/mfa.
    """


class GarminLoginRequiredError(SyncError):
    """Garmin needs the user's password: no saved session works and none was given.

    Also raised when Garmin rejects the password. Surfaces to the web UI as
    `needs_garmin_login: true` in the sync status, so the frontend asks for
    the password and POSTs /api/sync again with it.
    """
