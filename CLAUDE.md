# 7D2D Server Panel — Project Overview

## Architecture

Single Docker container. Flask panel (`app.py`) is the container's main process (PID 1 via `entrypoint.sh`). It manages the 7 Days to Die game server as a child subprocess via `subprocess.Popen`, enabling start/stop/restart/update from the browser without restarting the Docker container.

```
Container
├── Flask panel (port 8090)   ← always running
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
│   └── entrypoint.sh          # mkdir volumes, exec Flask
├── panel/
│   ├── app.py                 # All backend logic
│   ├── requirements.txt       # Flask + requests
│   └── templates/
│       ├── dashboard.html     # Server controls + stats
│       ├── login.html
│       ├── config.html        # sdtdserver.xml editor
│       ├── saves.html         # Save/world manager
│       ├── admin.html         # serveradmin.xml manager
│       ├── sandbox.html       # SandboxCode editor
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

## Key Environment Variables

| Variable | Default | Purpose |
|---|---|---|
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
Settings organized into sidebar sections defined in `_CONFIG_SECTIONS` dict.

**v3.0 note:** Difficulty, zombie speed, loot, etc. replaced by a single `SandboxCode` string. Generate in-game via New Game → Sandbox Options → Copy Code, then paste in the Sandbox page.

## Admin Manager

Full CRUD for `serveradmin.xml` with five sections: `users`, `whitelist`, `blacklist`, `commands`, `apitokens`.

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
