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

## Strava enrichment

Garmin activity names are often auto-generated. If you maintain better names
and sport types on Strava, wishmap can pull them in and override the Garmin
metadata when activities match (within ±5 min and ±5% distance).

1. Register an API app at https://www.strava.com/settings/api. Set
   "Authorization Callback Domain" to `localhost`.
2. Add a `[strava]` section to `wishmap.toml`:

   ```toml
   [strava]
   client_id = "12345"
   client_secret_pass = "Strava"   # or client_secret / client_secret_file
   ```

3. One-time auth: `uv run wishmap --strava-auth` — opens an authorization URL
   you visit in a browser; paste the `code` query param from the redirect URL
   back into the terminal. Tokens are saved to `~/.wishmap/strava_tokens.json`.
4. Populate the local DB: `uv run wishmap --sync-strava`. Re-run periodically.
5. `uv run wishmap --sync` (Garmin sync) automatically consults the Strava DB
   and overrides `name` and `sport` on matched routes; the `strava` tag is
   appended so you can filter on it.
