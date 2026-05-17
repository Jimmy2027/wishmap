"""Domain exceptions for wishmap sync operations."""


class SyncError(Exception):
    """Raised when a sync operation cannot complete.

    Carries a user-readable message (rate-limit guidance, auth-failed
    instructions, etc.) suitable for surfacing to the web UI or the CLI.
    """


class PassphraseRequiredError(SyncError):
    """`pass show` reached gpg-agent but no cached key was available.

    Surfaces to the web UI as `needs_passphrase: true` in the sync status
    so the frontend can prompt for the GPG passphrase and POST it to
    /api/unlock. Re-prompting is the recovery path.
    """


class BadPassphraseError(SyncError):
    """User-supplied passphrase did not decrypt the warm-up entry.

    Returned from POST /api/unlock as 401. Re-prompting with a correct
    passphrase is the recovery path.
    """


class LoopbackDisabledError(SyncError):
    """gpg-agent.conf has `no-allow-loopback-pinentry` (or equivalent).

    Returned from POST /api/unlock as 503. Re-prompting will not help —
    the user must change their gpg-agent configuration.
    """


class AgentUnreachableError(SyncError):
    """gpg-agent socket missing or unreachable.

    Returned from POST /api/unlock as 503. Re-prompting will not help —
    the user must fix their gpg-agent setup (start the daemon, set
    GNUPGHOME, etc.).
    """
