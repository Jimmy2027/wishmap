"""Secret resolution and gpg-agent unlock helpers.

Two responsibilities:

1. `resolve_secret` — read a secret three ways (direct value, file, or `pass`
   entry). Used by garmin and strava sync to fetch credentials at runtime.
2. `warm_gpg_agent` — decrypt one `pass` entry with a user-supplied passphrase
   via loopback pinentry. Side-effect only: gpg-agent caches the unlocked
   secret key for `default-cache-ttl` so subsequent `resolve_secret` calls
   succeed without prompting.

The classifier (`_classify_gpg_outcome`) parses GnuPG's machine-readable
status protocol (`--status-fd 2` plus stderr text) to distinguish:

- happy path (DECRYPTION_OKAY)
- bad passphrase (stderr "Bad passphrase" or `[GNUPG:] ERROR pkdecrypt_failed`
  with code 11)
- loopback rejected by gpg-agent (stderr "setting pinentry mode 'loopback'
  failed" or `[GNUPG:] ERROR set_pinentry_mode`)
- locked agent / no cached key on a read path (`[GNUPG:] NEED_PASSPHRASE` or
  `[GNUPG:] PINENTRY_LAUNCHED` with no DECRYPTION_KEY)
- everything else (generic SyncError with last line of stderr)

These signals were captured empirically against gpg 2.5; the keywords are
documented as part of the GnuPG status-fd protocol and are stable across
versions in a way stderr prose is not.

Security invariants:

- The passphrase is bound only as the local arg to `warm_gpg_agent`. It is
  passed to gpg via stdin (`input=passphrase`) and never logged, never
  echoed in exception messages, never returned.
- `warm_gpg_agent` redirects gpg's stdout (the decrypted plaintext) to
  DEVNULL so the secret never enters a Python string. We only need the
  side-effect on gpg-agent's cache, not the plaintext.
- Exception messages never include captured stderr verbatim — only
  classified, sanitized summaries.
"""

import os
import re
import subprocess
from pathlib import Path

from wishmap.exceptions import (
    AgentUnreachableError,
    BadPassphraseError,
    LoopbackDisabledError,
    PassphraseRequiredError,
    SyncError,
)


class _Outcome:
    OK = "ok"
    BAD_PASSPHRASE = "bad_passphrase"
    LOOPBACK_DISABLED = "loopback_disabled"
    PASSPHRASE_REQUIRED = "passphrase_required"
    AGENT_UNREACHABLE = "agent_unreachable"
    UNKNOWN = "unknown"


_STATUS_LINE_RE = re.compile(r"^\[GNUPG:\] (\w+)(?: (.*))?$", re.MULTILINE)


def _parse_status(stderr_text: str) -> set[str]:
    """Return the set of [GNUPG:] keywords seen in stderr."""
    return {m.group(1) for m in _STATUS_LINE_RE.finditer(stderr_text)}


def _classify_gpg_outcome(returncode: int, stderr_text: str) -> str:
    """Map a gpg subprocess result to one of the _Outcome constants.

    The order of checks matters: more specific signals before more general
    ones. Probe-derived (see design doc — empirical findings).
    """
    keywords = _parse_status(stderr_text)

    if returncode == 0 and "DECRYPTION_OKAY" in keywords:
        return _Outcome.OK

    # Most specific failure modes first.
    if "set_pinentry_mode" in stderr_text and "Not supported" in stderr_text:
        return _Outcome.LOOPBACK_DISABLED
    if "Bad passphrase" in stderr_text:
        return _Outcome.BAD_PASSPHRASE
    # gpg asked the user for a passphrase. Either we didn't supply one
    # (read path with cold agent) or loopback is set but the user hasn't
    # warmed yet. Either way: prompt the user.
    if "NEED_PASSPHRASE" in keywords or "PINENTRY_LAUNCHED" in keywords:
        return _Outcome.PASSPHRASE_REQUIRED
    # Non-zero exit with no specific signal we recognize. Most likely
    # gpg-agent unreachable or some other setup issue.
    if returncode != 0:
        return _Outcome.AGENT_UNREACHABLE

    return _Outcome.UNKNOWN


def _resolve_pass_entry_path(entry: str) -> Path:
    """Resolve a pass entry name to its underlying .gpg file path.

    Honors `$PASSWORD_STORE_DIR` (the canonical override pass uses);
    defaults to `~/.password-store/`.
    """
    base = os.environ.get("PASSWORD_STORE_DIR") or str(Path.home() / ".password-store")
    return Path(base).expanduser() / f"{entry}.gpg"


def warm_gpg_agent(entry: str, passphrase: str) -> None:
    """Decrypt one pass entry with loopback pinentry, discarding plaintext.

    Side-effect only: on success, gpg-agent caches the unlocked secret
    key for `default-cache-ttl`. Subsequent `resolve_secret` calls
    against entries encrypted to the same key succeed without prompting.

    Raises:
        BadPassphraseError: passphrase did not unlock the key.
        LoopbackDisabledError: gpg-agent rejected loopback pinentry.
        AgentUnreachableError: gpg-agent socket missing / cannot connect.
        SyncError: any other gpg failure (entry not found, etc.).
    """
    gpg_file = _resolve_pass_entry_path(entry)
    if not gpg_file.exists():
        raise SyncError(f"pass entry '{entry}' not found at {gpg_file}")

    try:
        result = subprocess.run(
            [
                "gpg",
                "--quiet",
                "--batch",
                "--pinentry-mode", "loopback",
                "--passphrase-fd", "0",
                "--status-fd", "2",
                "--decrypt", str(gpg_file),
            ],
            input=passphrase,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError as e:
        raise SyncError("'gpg' is not on PATH") from e

    outcome = _classify_gpg_outcome(result.returncode, result.stderr or "")

    if outcome == _Outcome.OK:
        return
    if outcome == _Outcome.BAD_PASSPHRASE:
        raise BadPassphraseError(
            f"Wrong passphrase for pass entry '{entry}'"
        )
    if outcome == _Outcome.LOOPBACK_DISABLED:
        raise LoopbackDisabledError(
            "gpg-agent rejected loopback pinentry. Add "
            "'allow-loopback-pinentry' to ~/.gnupg/gpg-agent.conf and run "
            "'gpgconf --reload gpg-agent'."
        )
    if outcome == _Outcome.AGENT_UNREACHABLE:
        raise AgentUnreachableError(
            "gpg-agent could not decrypt the entry and is not asking for a "
            "passphrase. Check that gpg-agent is running and GNUPGHOME is "
            "set correctly for this process."
        )
    if outcome == _Outcome.PASSPHRASE_REQUIRED:
        # We supplied a passphrase via loopback but gpg still wants one
        # via pinentry. Treat as a configuration issue similar to loopback
        # disabled — re-prompting won't help.
        raise LoopbackDisabledError(
            "gpg-agent did not accept the loopback passphrase. Check that "
            "'allow-loopback-pinentry' is set in ~/.gnupg/gpg-agent.conf "
            "and that 'gpgconf --reload gpg-agent' has been run."
        )
    # _Outcome.UNKNOWN — surface as generic error WITHOUT echoing stderr,
    # which may contain command-line fragments we don't want to render.
    raise SyncError(
        f"gpg failed to decrypt pass entry '{entry}' "
        f"(exit {result.returncode})"
    )


def resolve_secret(
    direct: str, file_path: str, pass_entry: str, label: str
) -> str:
    """Three-way secret resolver: direct value, file path, or pass entry.

    First non-empty input wins, in the order: direct, file_path, pass_entry.
    `label` is used only in error messages ("Garmin password" /
    "Strava client secret") so users can tell which credential failed.

    Raises:
        PassphraseRequiredError: pass entry exists but gpg-agent has no
            cached key and could not prompt (web UI should ask the user
            to unlock).
        SyncError: any other failure (pass not installed, entry missing,
            file unreadable, all three inputs empty).
    """
    if direct:
        return direct
    if file_path:
        return Path(file_path).expanduser().read_text().strip()
    if pass_entry:
        return _resolve_via_pass(pass_entry, label)
    raise SyncError(
        f"{label} config needs one of 'password'/'client_secret', "
        f"'password_file'/'client_secret_file', or "
        f"'password_pass'/'client_secret_pass'"
    )


def _resolve_via_pass(entry: str, label: str) -> str:
    """Call `pass show <entry>` with --batch --status-fd=2 injected.

    The --batch flag ensures gpg fails fast in a TTY-less environment
    rather than blocking on pinentry. --status-fd=2 gives us machine-
    readable signals for classification.
    """
    env = os.environ.copy()
    extra_opts = "--batch --status-fd=2"
    existing = env.get("PASSWORD_STORE_GPG_OPTS", "").strip()
    env["PASSWORD_STORE_GPG_OPTS"] = (
        f"{existing} {extra_opts}".strip() if existing else extra_opts
    )

    try:
        result = subprocess.run(
            ["pass", "show", entry],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError as e:
        raise SyncError(
            "'pass' is not on PATH — install passwordstore.org, use the "
            "*_file or direct-value config option, or remove the "
            "*_pass field."
        ) from e

    outcome = _classify_gpg_outcome(result.returncode, result.stderr or "")
    if outcome == _Outcome.OK:
        # pass show prints the first line of the entry as the password,
        # additional lines as metadata. We match `pass`'s own convention.
        stdout = result.stdout or ""
        lines = stdout.splitlines()
        if not lines:
            raise SyncError(
                f"pass entry '{entry}' decrypted to an empty value"
            )
        return lines[0].strip()
    if outcome == _Outcome.PASSPHRASE_REQUIRED:
        raise PassphraseRequiredError(
            f"gpg-agent has no cached key for pass entry '{entry}'. "
            f"Unlock via the web UI."
        )
    if outcome == _Outcome.LOOPBACK_DISABLED:
        # Caught for completeness — read path doesn't actually request
        # loopback, but a misconfigured gpg might surface it anyway.
        raise LoopbackDisabledError(
            "gpg-agent loopback configuration error. See "
            "~/.gnupg/gpg-agent.conf 'allow-loopback-pinentry'."
        )
    if outcome == _Outcome.AGENT_UNREACHABLE:
        raise AgentUnreachableError(
            f"gpg-agent could not read pass entry '{entry}'. Check that "
            "gpg-agent is running and GNUPGHOME is correct."
        )
    # _Outcome.UNKNOWN / _Outcome.BAD_PASSPHRASE (latter shouldn't occur
    # on a read path since the agent holds the key, but cover it).
    raise SyncError(
        f"pass show failed for entry '{entry}' (exit {result.returncode})"
    )
