"""Domain exceptions for wishmap sync operations."""


class SyncError(Exception):
    """Raised when a sync operation cannot complete.

    Carries a user-readable message (rate-limit guidance, auth-failed
    instructions, etc.) suitable for surfacing to the web UI or the CLI.
    """
