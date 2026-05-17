import logging
import os
import tomllib
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from wishmap.models import ServerSettings, WishmapConfig

logger = logging.getLogger(__name__)

# Single-user TOML keys the multi-user server no longer serves: pins,
# routes and the Garmin/Strava settings now live per account in the DB.
_LEGACY_DATA_KEYS = frozenset({"includes", "pins", "routes", "garmin", "strava"})


class ConfigError(Exception):
    """The TOML config is unusable (missing, invalid, or a GPX it names is missing)."""


def load_settings(path: Path, *, warn_legacy: bool = True) -> ServerSettings:
    """Load the server settings from `path`, raising ConfigError if unusable.

    Unknown keys are ignored with a warning, so an old single-user
    `wishmap.toml` still starts the server: its pins/routes are simply not
    served (they belong in an account now) — one note names them all and
    how to import them. `warn_legacy=False` silences that note for the
    import that is moving them into an account.
    """
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"Config file not found: {path}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"Invalid TOML in {path}: {e}") from e

    known = set(ServerSettings.model_fields)
    unknown = data.keys() - known
    # One note for all legacy keys: a kept single-user TOML has several, and
    # a warning per key would read like several problems.
    legacy = sorted(unknown & _LEGACY_DATA_KEYS)
    if legacy and warn_legacy:
        logger.warning(
            "%s: not serving its single-user data (%s) — pins, routes and "
            "Garmin/Strava settings are stored per account now. Move them into "
            "one with `wishmap import-toml --user NAME %s`; the keys are "
            "ignored until removed.",
            path,
            ", ".join(legacy),
            path,
        )
    for key in sorted(unknown - _LEGACY_DATA_KEYS):
        logger.warning("%s: ignoring unknown key %r", path, key)

    try:
        return ServerSettings(**{k: v for k, v in data.items() if k in known})
    except ValidationError as e:
        raise ConfigError(f"Invalid settings in {path}: {e}") from e


def load_user_toml(path: Path) -> WishmapConfig:
    """Load and validate a single-user wishmap TOML (the input of `import-toml`).

    Resolves `includes` (merging their pins/routes; a missing include is
    skipped, as the single-user app did). Every route's `gpx` path — in the
    file itself and in its includes — is relative to `path`'s directory.
    Raises ConfigError if a file is unreadable or invalid, or a route's GPX
    file is missing.
    """
    data = _read_toml(path)
    base = path.parent

    for include_rel in data.get("includes", []):
        include_path = base / include_rel
        if not include_path.is_file():
            logger.warning("%s: include %s not found, skipping", path, include_path)
            continue
        inc_data = _read_toml(include_path)
        data.setdefault("pins", []).extend(inc_data.get("pins", []))
        data.setdefault("routes", []).extend(inc_data.get("routes", []))

    try:
        config = WishmapConfig(**data)
    except ValidationError as e:
        raise ConfigError(f"Invalid config {path}: {e}") from e

    for route in config.routes:
        gpx_path = base / route.gpx
        if not gpx_path.is_file():
            raise ConfigError(f"GPX file not found: {gpx_path}")

    return config


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except OSError as e:
        raise ConfigError(f"Cannot read config {path}: {e}") from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"Invalid TOML in {path}: {e}") from e


def resolve_data_root(config_path: Path, settings: ServerSettings) -> Path:
    """The server's data directory: `data_dir`, relative to the config's directory."""
    return config_path.parent / settings.data_dir


def resolve_config_path() -> Path:
    """Resolve config path from env var or default."""
    env = os.environ.get("WISHMAP_CONFIG")
    if env:
        return Path(env)
    return Path("wishmap.toml")
