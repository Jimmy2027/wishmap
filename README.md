# wishmap

Self-hosted map with pins, routes, and filtering for outdoor sports.

Displays an OpenStreetMap basemap with pins (points of interest) and routes
(loaded from GPX files). Configured entirely via a TOML file.

## Setup

```bash
uv sync
```

## Run

```bash
uv run wishmap
```

Then open http://127.0.0.1:8000

### Options

```bash
uv run wishmap --config /path/to/config.toml
uv run wishmap --host 0.0.0.0 --port 3000
```

## Configuration

Edit `wishmap.toml` to add pins and routes:

```toml
title = "wishmap"

[[pins]]
id = "pin-1"
name = "Nice hut"
lat = 46.835
lon = 9.825
sport = ["hiking", "ski_touring"]
status = "idea"
tags = ["hut", "viewpoint"]
notes = "Could be a good overnight stop"

[[routes]]
id = "route-1"
name = "Weekend gravel loop"
sport = "biking"
status = "planned"
tags = ["gravel", "loop"]
notes = "Try in late spring"
color = "#2E86DE"
gpx = "data/routes/weekend-gravel-loop.gpx"
```

Routes reference GPX files. Paths are relative to the TOML file location.

### Fields

| Field | Required | Description |
|-------|----------|-------------|
| `id` | yes | Unique identifier |
| `name` | yes | Display name |
| `sport` | yes | String or list of strings |
| `status` | yes | `idea`, `planned`, or `done` |
| `lat`/`lon` | pins only | Coordinates |
| `gpx` | routes only | Path to GPX file |
| `tags` | no | List of strings |
| `notes` | no | Free text |
| `color` | routes only | Hex color for the route line |
