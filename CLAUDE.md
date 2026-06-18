# 7D2D Server Panel — Project Overview

## Architecture

Single Docker container. Flask panel (`app.py`) is the container's main process (PID 1 via `entrypoint.sh`). It manages the 7 Days to Die game server as a child subprocess via `subprocess.Popen`, enabling start/stop/restart/update from the browser without restarting the Docker container.

```
Container (entrypoint starts as root, drops to PUID:PGID via gosu)
├── Flask panel (port 8090)   ← always running as service user
│   ├── spawns → 7DaysToDieServer.x86_64
│   └── reads stdout → log buffer → SSE → browser
└── SteamCMD (on demand)      ← install/update subprocess
```

## Directory Layout

```
/srv/7d2d-server/
├── Dockerfile
├── docker-compose.yml
├── scripts/
│   └── entrypoint.sh          # creates user at PUID:PGID, chowns volumes, exec gosu → Flask
├── panel/
│   ├── app.py                 # All backend logic
│   ├── requirements.txt       # Flask + requests
│   └── templates/
│       ├── _api_warning.html  # Shared banner partial — included by all pages
│       ├── dashboard.html     # Server controls + stats
│       ├── login.html
│       ├── config.html        # sdtdserver.xml editor
│       ├── saves.html         # Save/world manager
│       ├── admin.html         # serveradmin.xml manager
│       ├── sandbox.html       # Visual SandboxCode editor
│       ├── console.html       # Live log stream + command input
│       ├── players.html       # Online players + actions
│       ├── give.html          # Give items
│       └── teleport.html      # Waypoint browser + bot config
└── data/                      # Runtime data (gitignored)
    ├── serverfiles/           # Game installation (SteamCMD target)
    ├── gamedata/              # Saves, worlds, serveradmin.xml
    └── config/                # sdtdserver.xml, teleport_data.json
```

## Volume Mounts (container paths)

| Host | Container | Contents |
|---|---|---|
| `./data/serverfiles` | `/serverfiles` | Game binaries installed by SteamCMD |
| `./data/gamedata` | `/gamedata` | Saves, GeneratedWorlds, serveradmin.xml |
| `./data/config` | `/config` | sdtdserver.xml, teleport_data.json |
| `./data/logs` | `/logs` | Game server log files |

## Key Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `PUID` | `1000` | UID the service process runs as |
| `PGID` | `1000` | GID the service process runs as |
| `PANEL_PASSWORD` | `admin` | Web panel login password |
| `SECRET_KEY` | random | Flask session signing key |
| `GAME_BRANCH` | `public` | `public` or `latest_experimental` |
| `GAME_API_URL` | `http://localhost:8080` | Game REST API base (same container) |
| `GAME_API_TOKEN_NAME` | — | Token name from serveradmin.xml |
| `GAME_API_SECRET` | — | Token secret from serveradmin.xml |
| `CONFIG_PATH` | `/config/sdtdserver.xml` | Active server config |
| `ADMIN_PATH` | `/gamedata/Saves/serveradmin.xml` | Admin XML |
| `SAVES_ROOT` | `/gamedata/Saves` | Saves directory |
| `WORLDS_ROOT` | `/gamedata/GeneratedWorlds` | RWG worlds directory |
| `TELEPORT_DATA_PATH` | `/config/teleport_data.json` | Teleport bot data |
| `SERVERFILES_PATH` | `/serverfiles` | Game installation directory |
| `STEAMCMD_PATH` | `/opt/steamcmd/steamcmd.sh` | SteamCMD script |
| `LOG_DIR` | `/logs` | Directory for game server log files |

## Privilege Drop (PUID/PGID)

The container runs as root to start. `entrypoint.sh` creates a group/user matching `PGID`/`PUID` (skipped if they already exist), chowns `/serverfiles`, `/config`, `/gamedata`, `/opt/steamcmd`, and `/opt/panel`, then uses `exec gosu ${PUID}:${PGID}` to drop privileges before Flask starts. All file I/O after that point runs as the service user. `PUID`/`PGID` are runtime env vars — no rebuild needed to change them.

## Server Process Management (`app.py`)

### State machine
`_server_state` global: `stopped → starting → running → stopping → stopped`
Also: `installing` (SteamCMD running, blocks start/stop).

### Key globals
- `_server_proc` — `subprocess.Popen` handle, `None` when stopped
- `_server_state` — current state string, guarded by `_server_lock`
- `_log_buffer` — `deque(maxlen=500)` of log entry dicts
- `_log_subs` — list of `queue.Queue` objects, one per SSE client

### Server startup
```python
server_start()  # launches 7DaysToDieServer.x86_64
                # starts _proc_reader thread
                # state: stopped → starting
```
`_proc_reader` reads stdout line by line:
- Pushes every line to `_log_buffer` + all SSE subscriber queues
- On `"Started Webserver on port"` → transitions state to `running`
- On chat lines → calls `_handle_chat()` in a new thread
- On process exit → sets state back to `stopped`

### Log file tee

Every line of game server stdout is written to `LOG_DIR/server.log` (line-buffered, raw format with timestamp prefix) in addition to being pushed to the SSE buffer. On each `server_start()` call, `_rotate_server_log()` runs first: if `server.log` exists and is non-empty, it is renamed to `server-YYYY-MM-DD-HHMMSS.log` (using the file's mtime) and a fresh `server.log` is opened. The file handle is stored in `_log_file` (global, guarded by `_log_file_lock`) and closed by `_close_server_log()` in the `_proc_reader` finally block.

From the host: `tail -f ./data/logs/server.log`

### SIGTERM handler
Registered for both SIGTERM and SIGINT. Terminates the game server subprocess (with 60s timeout before SIGKILL), then calls `os._exit(0)`. This allows `docker compose down` to complete in well under the 75s `stop_grace_period`.

### Branch detection
`_installed_branch()` reads `steamapps/appmanifest_294420.acf` and regex-searches for `"betakey"` using `re.IGNORECASE` — the manifest writes `"BetaKey"` with mixed case.

### Log format
Raw game output: `2026-01-01T00:00:00 123.456 INF message text`
Parsed by `_parse_raw()` into `{"msg": "...", "type": "Log|Warning|Error|Exception", "isotime": "..."}`.

### SSE log stream
`GET /api/log-stream` — sends last 100 buffered entries on connect, then streams new entries as `event: logLine` with JSON data. The console template listens with `es.addEventListener('logLine', ...)`.

## Game API

The game runs its REST API on `http://localhost:8080` (same container, no network hop). Requires `X-SDTD-API-TOKENNAME` + `X-SDTD-API-SECRET` headers.

Key endpoints used by the panel:

| Endpoint | Purpose |
|---|---|
| `GET /api/player` | Online players — returns `data.players[]` |
| `GET /api/gamestats` | Game day, player count |
| `GET /api/serverstats` | FPS, memory |
| `POST /api/command` | Execute console command |
| `GET /api/item` | Full item catalogue (~26k entries) |

**Player API note:** Array is at `data.players`, not `data` directly.
**Item search:** API ignores searchterm — fetch all, filter client-side.

## Teleport Bot

Chat regex: `Chat \(from '([^']+)', entity id '(\d+)', to '([^']*)'\): '([^']*)': (.*)`
Entity ID is quoted in v3.0 log format.

`sayplayer` requires message in double quotes to handle spaces:
`sayplayer 171 "multi word message"`

Waypoints stored in `teleport_data.json` keyed by Steam ID (`Steam_XXXXXXXXXXXXXXXXX`).
Player names are saved on any `!` command and backfilled from the player cache on `/api/waypoints` for currently-online players.

**In-game commands:** `!settele`, `!tele`, `!deltele`, `!listtele`

Bot config (editable in Teleport page): `cooldown_seconds`, `daily_limit` (0 = unlimited), `max_waypoints_per_player`.

## Config Editor

Reads/writes `/config/sdtdserver.xml` in v3.0 `<property name="..." value="..."/>` format.

Settings are organized via `_SECTIONS` (a list of dicts with `id`, `label`, `icon`, `fields`). Field rendering is driven by three companion dicts:
- `_FIELD_TYPES` — `text | number | boolean | select | sandbox_code`
- `_FIELD_META` — per-field `label` and `options` list (for selects)
- `_FIELD_DESCRIPTIONS` — optional helper text shown below the input

The config route builds `meta` as a shallow copy of `_FIELD_META` with `GameWorld.options` populated dynamically by `_available_worlds()`, which scans `SERVERFILES_PATH/Data/Worlds/` and falls back to a hardcoded list if the server isn't installed yet.

`WorldGenSize` is a select with three options (6144 / 8192 / 10240) — the only officially supported RWG sizes.

`SandboxCode` uses the `sandbox_code` field type, which renders a monospace textarea with a *Reload Saved* button (re-fetches from disk) and a description pointing to the Sandbox tab for full editing and reset.

### WebDashboardEnabled warning
`WebDashboardEnabled` has `"fix_when_false": True` in `_FIELD_META`. When the saved value is false/0/empty, the `render_field` macro applies `.field-warning` red styling to the field card and renders a Fix button that opens a confirmation modal. The modal POSTs `{"updates": {"WebDashboardEnabled": "true"}}` to `/api/config` and prompts the user to restart.

A separate full-width banner (`_api_warning.html`) is included by every page (except login) directly below the navbar. It fetches `/api/server/status` on page load; if `api_enabled` is false it shows an amber strip with a **Fix Now** button (same POST as the modal) and a link to Config. The banner hides itself after a successful fix or when dismissed.

### Config API
- `GET /api/config` — returns current config values as a flat JSON dict
- `POST /api/config` — accepts `{"updates": {"Key": "value", ...}}` and merges into the existing XML

## Sandbox Editor

Full visual editor for v3.0 SandboxCode. 150+ options across 8 categories (Player, Entities, World, Resources, Crafting, Tasks, Traders, Misc), organized into groups. Each option stores a value index into a `vals[]` array; `def` is the default index.

### Encode/decode
- `encode()` — builds code string from non-default state entries: `A` prefix + 3-char triplets `[enumId_hi][enumId_lo][valueIdx]` (all base-26, A=0)
- `decode(code)` — parses triplets back into state indices

On page load: if URL hash is a valid code, load it (shared/bookmarked state takes priority). Otherwise, auto-fetch `GET /api/config` and decode the saved `SandboxCode` so the editor always opens with your current settings.

## Admin Manager

Full CRUD for `serveradmin.xml` with five sections: `users`, `whitelist`, `blacklist`, `commands`, `apitokens`.

The `/admin` route must pass **both** `data=parse_admin()` and `tele_data=_load_tele()` to the template — the template uses `data` for the five admin tabs and `tele_data` for the teleport section.

Routes:
- `GET /api/admin` — returns all sections as JSON
- `POST /api/admin/<section>` — append entry
- `DELETE /api/admin/<section>` — remove by criteria (JSON body with matching key/value pairs)

Permission levels: `0` = full admin, `1000` = default player.

## Deploying Changes

Panel code is baked into the image. To deploy:

```bash
git pull && docker compose up -d --build
```

The game server process is killed when the container restarts. Start it again from the Dashboard after the rebuild.

## v3.0 API Notes

- SSE endpoint: `/sse/?events=log` with `events` query param (not `/sse/log`)
- SSE event name: `logLine` (not the default `message`)
- Alloc Fixes (`TFP_WebServer`) is **incompatible with v3.0** — keep disabled
- Loot stage / game stage not exposed by v3.0 API (was Allocs-specific)
- `appmanifest_294420.acf` uses `"BetaKey"` (mixed case) — always use `re.IGNORECASE` when parsing it
