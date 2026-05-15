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

## Garmin sync

Wishmap can pull your Garmin Connect activities, download their GPX tracks, and
generate route entries automatically.

1. Add a `[garmin]` section to `wishmap.toml`:

   ```toml
   [garmin]
   username = "you@example.com"
   password_pass = "Garmin"     # or password / password_file

   # Optional:
   # activity_limit = 1000        # how many recent activities to fetch
   # gpx_dir = "data/garmin"      # where GPX files are stored (relative to TOML)
   # tokenstore = "~/.garminconnect"
   # sport_filter = ["hiking", "cycling", "trail_running"]  # only these Garmin typeKeys
   ```

   Provide the password through exactly one of:
   - `password` — plaintext (not recommended)
   - `password_file` — path to a file containing the password
   - `password_pass` — entry name in [passwordstore](https://www.passwordstore.org/)
     (`pass show <name>`)

2. Include the auto-generated routes file from your main config:

   ```toml
   includes = ["data/garmin/garmin.toml"]
   ```

3. Run the sync:

   ```bash
   uv run wishmap --sync
   ```

   On first run, Garmin Connect is contacted with your credentials and a token
   is cached to `tokenstore` (`~/.garminconnect` by default). Subsequent syncs
   reuse the token until it expires. GPX files are written to `gpx_dir` and a
   `garmin.toml` file with one `[[routes]]` entry per activity is regenerated.

Only activities with GPS data are included. Garmin activity types are mapped to
wishmap sports (e.g. `cycling` → `biking`, `backcountry_skiing_snowboarding` →
`ski_touring`); unknown types pass through as-is with a default gray color.

If Garmin rate-limits you (`429`), wait 15–60 minutes before retrying.

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
