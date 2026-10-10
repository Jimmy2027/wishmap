# wishmap

Self-hosted map of outdoor routes and pins (hiking, biking, ski touring, …),
for one person or a handful of friends on one server.

Everyone gets an account with their own map on an OpenStreetMap basemap:
pins (places to go), routes (GPX tracks — added by hand, synced from Garmin
Connect, or imported), star ratings, and filters by sport and status. Users
can share their routes with the other accounts on the server, and a map can
be shared read-only through a public link, or exported as a static site.

## Setup

```bash
uv sync
```

## Run

```bash
uv run wishmap
```

Then open http://127.0.0.1:8000, register an account and log in.

`uv run wishmap` (the same as `uv run wishmap serve`) is the development
server: it binds to `127.0.0.1:8000` and reloads on code changes. For a server
other people reach, see [Deployment](#deployment).

### Options

```bash
uv run wishmap --host 0.0.0.0 --port 3000
uv run wishmap --config /path/to/wishmap.toml   # or set WISHMAP_CONFIG
uv run wishmap import-toml --user NAME wishmap.toml   # see "Moving a single-user install …"
uv run wishmap export --user NAME ./public            # see "Static export"
```

## Configuration

`wishmap.toml` (in the working directory, or wherever `--config` /
`WISHMAP_CONFIG` points) holds the server-wide settings only. Everything that
belongs to a person — pins, routes, ratings, Garmin and Strava settings — lives
in their account and is edited in the web UI.

```toml
title = "wishmap"           # shown in the page title and header
allow_registration = true   # anyone who reaches the server can create an account
secure_cookies = false      # true when the server is reached over HTTPS
session_days = 30           # how long a login lasts (1–365)
data_dir = "data"           # app state; relative to this file unless absolute
```

| Key | Default | Description |
|-----|---------|-------------|
| `title` | `"wishmap"` | Page title |
| `allow_registration` | `true` | Offer "Register" on the login screen. Turn off once everyone has an account; existing accounts keep logging in |
| `secure_cookies` | `false` | Mark the login cookies `Secure` (HTTPS only). Must be `true` in production — see [Deployment](#deployment) |
| `session_days` | `30` | Lifetime of a login and its cookies, in days |
| `data_dir` | `"data"` | Where `wishmap.db` and the per-account files live |

Settings are read at startup; restart the server after changing them. The
file must exist (it may be empty). Keys the server doesn't know are ignored
with a warning. The keys of a single-user config from before accounts
(`pins`, `routes`, `includes`, `[garmin]`, `[strava]`) are ignored with one
startup note — they are the input of `import-toml`, not served (see
[Moving a single-user install into an account](#moving-a-single-user-install-into-an-account)).

The `wishmap.toml` in this repository still carries such a block below its
settings: it is the author's pre-account install, kept so that
`import-toml` can move it into an account. A fresh install only needs the
settings.

## Accounts

The login screen has **Log in** and, while `allow_registration` is on,
**Register**. Usernames are 3–32 characters (letters, digits, `.`, `_`, `-`,
case-insensitive); passwords need at least 8 characters.

- **Passwords** are stored as salted scrypt hashes. A login sets two
  HttpOnly cookies: `wishmap_session` (a random token; the database keeps
  only its SHA-256) and `wishmap_vault` (the account's vault key, see
  [Security model](#security-model)).
- **Brute-force brake**: after 5 wrong passwords for a username, that name
  is locked out for 60 seconds (HTTP 429), even for the right password.
  The counter is in memory; a restart resets it.
- **Settings → Account** changes the password (which signs out the
  account's other sessions) and logs out.
- **There is no password reset.** A forgotten password can't be recovered
  by the user; because the vault key is wrapped under the password, a reset
  would lose the saved Garmin login anyway (the map data would survive).
- With registration closed, `import-toml` (below) can still create an
  account on the command line.

Every write endpoint refuses cross-site requests (`Sec-Fetch-Site`, falling
back to `Origin`), so another site can't act through a logged-in browser.

## Using the map

- **＋ Add route**: paste a Strava route link (optional) and upload its
  exported GPX (up to 10 MB), then set name, sport, status and tags.
  A public **Ride with GPS** trip or route link
  (`https://ridewithgps.com/trips/…` or `/routes/…`) needs no file: the
  server downloads the track and takes its name (unless you give one) and
  description. Importing the same link again replaces the route. Private
  items can't be fetched this way — download their GPX and upload it.
- **＋ Add pin**: click the button, then the map where the pin goes.
- **Edit / delete**: the ✎ Edit and ✕ buttons in a route's or pin's popup.
  A route's GPX can't be edited — delete the route and add it again.
  Route-start markers follow their route.
- **Ratings**: each route can be rated 1–5 on Fun, Difficulty and Scenery
  in its popup; the ☰ Routes drawer sorts by any axis.
- **Filters** by sport and status.

| Field | Description |
|-------|-------------|
| `name` | Display name |
| `sport` | One or more sports (`hiking`, `biking`, `ski_touring`, …; free text) |
| `status` | `idea`, `planned` or `done` |
| `tags` | Free-form labels |
| `notes` | Free text (a Garmin sync writes date, distance and duration here) |

Colors are not configurable: each sport gets its own color, and `idea`
pins and routes render at reduced opacity (a faint "draft" look). The color
map plus a hashed fallback for unmapped sports lives in
`src/wishmap/static/index.html`.

## Garmin sync

The **↻ Sync** button pulls your Garmin Connect activities with GPS data,
downloads their GPX tracks and puts them on your map as `done` routes.

1. **⚙ Settings → Garmin Connect**: enter your Garmin email, how many of
   the newest activities to sync (default 1000), and optionally which
   Garmin activity types to keep (e.g. `hiking, cycling`). Save. (Or skip
   this: the first sync asks for the email too, with the default settings.)
2. Click **↻ Sync**. The first time, wishmap asks for your **Garmin
   password**. It is sent with that one sync, used for the login and then
   dropped — never written to disk or logged.
3. If your Garmin account uses two-factor login, Garmin sends a one-time
   code by email or SMS; enter it in the **Garmin MFA code** dialog and the
   sync restarts.
4. The Garmin session the login produced is saved **encrypted with your
   vault key** (see [Security model](#security-model)). Later syncs use it
   without asking for the password — from any browser you log in on —
   until Garmin expires it, Garmin rejects it, or you click **Forget Garmin
   login**. Then the password dialog comes back.

Sync runs in the background; the map refreshes when it finishes. Hover the
Sync button after a failed sync to see the error. If Garmin rate-limits you
(`429`), wait 15–60 minutes before retrying.

What a sync writes: one route `garmin-<activityId>` per activity, its GPX at
`gpx/garmin/<activityId>.gpx` in your account's directory. Each sync
**replaces your Garmin routes as a set**: an activity that falls outside the
activity limit (or the type filter) drops off the map, and edits made to a
Garmin route in the web UI are overwritten by the next sync. Ratings are
keyed on the stable route id, so they survive re-syncs. Routes you added
yourself or imported are never touched.

Garmin activity types are mapped to wishmap sports (e.g. `cycling` →
`biking`, `backcountry_skiing_snowboarding` → `ski_touring`); unknown types
pass through as-is and get a stable hashed color.

A browser that logged in before the vault existed, or whose `wishmap_vault`
cookie is gone, can still use the map but not the saved Garmin login: Sync
asks you to log in to wishmap again (which restores the vault key).

## Strava names (archive upload)

Garmin activity names are often auto-generated. If you keep better names
and sport types on Strava, wishmap uses them for Garmin routes whose
activity matches a Strava one (start within ±5 min, distance within ±5%), and
links the route to the Strava activity.

Strava's API is subscriber-only, so wishmap reads Strava's data export:

1. On strava.com: Settings → My Account → "Download or Delete Your Account"
   → "Request your archive". Strava emails a `.zip`.
2. **⚙ Settings → Strava**: upload the `.zip` (or just its
   `activities.csv`).

The upload fills your account's Strava activity database and immediately
re-matches your existing Garmin routes, so new names show up without another
Garmin sync. Upload a fresh archive whenever you want newer activities
matched; it merges with what's already there. No Strava login or token is
involved.

## Public links

**⚙ Settings → Public link → Create link** publishes your map at
`https://<your server>/u/<token>`: the same map, read-only, for anyone with
the link and without an account. It is a live view — later changes show up
there too.

- **Rotate** replaces the token: the old URL stops working at once.
- **Revoke** takes the link down. An unknown or revoked token is a plain
  404 that doesn't say whether the link ever existed.
- The page is sent with `Referrer-Policy: strict-origin` (tile servers and
  sites you link to see only your server's origin, never the token) and
  `X-Robots-Tag: noindex`.
- Everything on your map is visible there: every pin and route, its notes
  and tags, and your ratings. There are no per-item privacy settings.
- The link is built from the URL the server sees; behind a reverse proxy,
  it is only `https://` and on the right host if the proxy is set up as in
  [Deployment](#deployment).

## Sharing routes with other users

**⚙ Settings → Share with other users → Make my routes available to other
users on this server** opts your routes in. Every other account on the
server can then overlay them on its own map; there is no per-user list of
who may see them.

- Others see each route's track, name, sport, status and tags. Notes,
  Strava ids, ratings and pins are never shared.
- Every route is shared, including synced Garmin activities. Those usually
  start at your home, and the tracks are not trimmed.
- Opting out takes effect at once: your routes can no longer be fetched,
  and you are no longer offered after the others reload the page.
- In the **Other users** group of the legend & filters panel, tick whose
  routes to show. Each user gets their own color; their routes follow the
  sport and status filters but never feed the heatmap, and are not editable
  or rateable. The selection is remembered in the browser.
- Public links and static exports never include other users' routes.

## Static export

```bash
uv run wishmap export --user NAME ./public
```

writes a read-only snapshot of one account's map as a static site:
`index.html` plus `api/{config,pins,routes,ratings}.json`. It needs no
server, has no write surface and contains no credentials, so it can go to
any static host (it works under a sub-path). Unlike a public link it is a
snapshot: re-run the export to update it. Route geometry is simplified a
little to keep the files small, and the Google Fonts import is dropped (the
page falls back to system fonts) so the host doesn't leak viewers' IPs to
Google.

## Moving a single-user install into an account

Before accounts, wishmap was configured by one `wishmap.toml` with
`[[pins]]`, `[[routes]]`, `includes` (the `data/garmin/garmin.toml` a Garmin
sync generated), `[garmin]` and `[strava]` sections, and kept web-added
routes and ratings in `data/wishmap.db`. The server doesn't serve any of that
anymore. To move it into an account:

1. Start the new version once (or not — the import does the same): it
   migrates `data/wishmap.db` in place, parking the old web routes and
   ratings as *unclaimed*.
2. Stop the server, then run

   ```bash
   uv run wishmap import-toml --user hendrik wishmap.toml
   ```

   This creates the account if it doesn't exist (asking for its password;
   or set `WISHMAP_PASSWORD`, or pass `--password-stdin`) and imports:

   - the TOML's pins, and its routes with their includes; each GPX file is
     **copied** into the account's directory (`gpx/garmin/` for Garmin-synced
     `garmin-*` routes, `gpx/import/<original path>` for the rest);
   - the unclaimed web routes and ratings from the old database — only when
     importing the server's own config (the one `--config` /
     `WISHMAP_CONFIG` names), since those belong to that install; `--claim` /
     `--no-claim` override this;
   - the Strava activities database (`data/strava/activities.db`);
   - the `[garmin]` username, `activity_limit` and `sport_filter` — never a
     password, password file or token. The first sync asks for the Garmin
     password once.

   Re-running is safe: routes and pins are replaced by id (the TOML wins
   over edits made since), up-to-date GPX files are not copied again, and
   Strava activities merge. The source files are only read.

3. Start the server. Once you're happy, delete the legacy keys from
   `wishmap.toml`; the old `data/garmin/`, `data/routes/` and `data/strava/`
   aren't read by the server anymore either.

Running the import against a live server works, but an account whose map the
server has already loaded keeps showing the old data until a restart.

## Security model

What the server stores, per account, in `data/wishmap.db`:

- the password as a salted **scrypt** hash, and session tokens only as
  SHA-256 hashes — a copy of the database can't be replayed as a login;
- a random 32-byte **vault key**, only *wrapped*: AES-GCM-encrypted under a
  key derived from the password with scrypt. Login unwraps it and hands it to
  the browser in the `wishmap_vault` cookie (HttpOnly, SameSite=Strict); the
  server holds it in the clear only while handling a request that carries
  it. A password change re-wraps it;
- the **Garmin session tokens**, AES-GCM-sealed with the vault key and bound
  to the account and provider. The Garmin *password* is never stored.

What that protects against, and what not:

- **A stolen database** (or backup) alone yields the Garmin sessions only by
  brute-forcing a user's wishmap password through scrypt, one account at a
  time. A weak password makes that feasible; a strong one doesn't.
- **A compromised running server** — or its operator — sees what passes
  through it: wishmap passwords at login, Garmin passwords during a sync, and
  the vault key of every request that carries the cookie. The vault is not a
  defence against the host.
- **A stolen browser cookie jar** holds both the session and the vault key
  (until the session expires or the password changes).
- **Plain HTTP** exposes cookies and passwords to the network. Serve over
  HTTPS with `secure_cookies = true`.
- **Map data is not encrypted**: pins, routes, notes, ratings, GPX files and
  the Strava activity list are plain files and rows the operator can read.
- **Sharing with other users** is opt-in and all or nothing: it shows your
  routes (track, name, sport, status, tags) to every account on the server.
  Notes, ratings and pins stay private.
- **Forgetting the password** loses the vault key, so the saved Garmin login
  is gone (log in to Garmin again); the account's map data is unaffected.

## Deployment

wishmap holds per-user state (built maps, sync status, a Garmin login waiting
for its MFA code, the login throttle) in memory, so it must run as **a single
process** — one uvicorn worker. Put it behind a reverse proxy that
terminates TLS, and run uvicorn directly (not `wishmap serve`, which
auto-reloads):

```bash
WISHMAP_CONFIG=/srv/wishmap/wishmap.toml \
  uv run uvicorn wishmap.app:app --host 127.0.0.1 --port 8000 \
    --proxy-headers --forwarded-allow-ips 127.0.0.1
```

and in `wishmap.toml`:

```toml
secure_cookies = true
allow_registration = false   # once everyone has an account
data_dir = "/srv/wishmap/data"
```

- `--proxy-headers` makes uvicorn trust `X-Forwarded-Proto` / `-For` from
  the addresses in `--forwarded-allow-ips` (the proxy's; `127.0.0.1` when it
  runs on the same host). Without them the app thinks it is on plain
  `http://`, and public links come out as `http://` URLs.
- The proxy must pass the original **`Host`** header: public links use it,
  and the cross-site check compares a request's `Origin` against it. Caddy
  does this by default; nginx needs `proxy_set_header Host $host;`.
- `secure_cookies = true` keeps the session and vault cookies off plain HTTP.
  Leave it off for a plain-HTTP development server — browsers would not send
  the cookies back and you could never stay logged in.
- Raise the proxy's request-size limit if you want to upload Strava exports
  (often 100 MB+; wishmap accepts up to 1 GiB) or large GPX files (10 MB).

Example Caddyfile:

```
map.example.com {
	reverse_proxy 127.0.0.1:8000
}
```

Example nginx server block:

```nginx
server {
    listen 443 ssl;
    server_name map.example.com;
    # ssl_certificate …; ssl_certificate_key …;
    client_max_body_size 1g;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 300s;   # an account's first map load parses all its GPX
    }
}
```

Example systemd unit:

```ini
[Service]
User=wishmap
WorkingDirectory=/srv/wishmap/app
Environment=WISHMAP_CONFIG=/srv/wishmap/wishmap.toml
ExecStart=/usr/bin/uv run uvicorn wishmap.app:app --host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips 127.0.0.1
Restart=on-failure
```

An account's map is built on its first request after a (re)start, which
parses all of its GPX files — up to a minute for a thousand-route account.
Later requests are served from memory.

## Data layout

Everything lives under `data_dir`:

```
data/
  wishmap.db                 accounts, sessions, pins, routes, ratings,
                             integrations, public links, sharing
                             (SQLite, WAL)
  users/<account id>/
    gpx/web/…                routes added in the web UI
    gpx/garmin/…             Garmin sync (+ activities.json, the last sync's
                             activity list, for Strava re-matching)
    gpx/import/…             routes from import-toml
    strava/activities.db     the uploaded Strava activities
```

The database schema is created — and an older database migrated — whenever
it's opened; there is no setup step. To back up, copy `data/users/` and take
a consistent copy of the database:

```bash
sqlite3 data/wishmap.db ".backup /backups/wishmap.db"
```

Inspect it with the `sqlite3` CLI, e.g. an account's best routes:

```bash
sqlite3 data/wishmap.db \
  "SELECT r.name, x.fun FROM route_ratings x
   JOIN routes r ON r.user_id = x.user_id AND r.id = x.route_id
   JOIN users u ON u.id = x.user_id
   WHERE u.username = 'hendrik' ORDER BY x.fun DESC LIMIT 10"
```
