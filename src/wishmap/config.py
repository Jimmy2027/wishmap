import os
import sys
import tomllib
from pathlib import Path

from wishmap.models import WishmapConfig


def load_config(path: Path) -> WishmapConfig:
    """Load and validate the wishmap TOML config."""
    with open(path, "rb") as f:
        data = tomllib.load(f)

    base = path.parent

    # Resolve includes: merge pins/routes from included TOML files
    for include_rel in data.get("includes", []):
        include_path = base / include_rel
        if not include_path.is_file():
            continue
        with open(include_path, "rb") as f:
            inc_data = tomllib.load(f)
        data.setdefault("pins", []).extend(inc_data.get("pins", []))
        data.setdefault("routes", []).extend(inc_data.get("routes", []))

    config = WishmapConfig(**data)

    for route in config.routes:
        gpx_path = base / route.gpx
        if not gpx_path.is_file():
            print(f"Error: GPX file not found: {gpx_path}", file=sys.stderr)
            sys.exit(1)

    return config


def resolve_config_path() -> Path:
    """Resolve config path from env var or default."""
    env = os.environ.get("WISHMAP_CONFIG")
    if env:
        return Path(env)
    return Path("wishmap.toml")
