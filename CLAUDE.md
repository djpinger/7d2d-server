# 7D2D Server Panel — Project Overview

## Architecture

Two Docker containers. The panel container manages the game container via the Docker SDK, so rebuilding the panel does not affect the running game server.

```
7dtd-panel (Dockerfile)              7dtd-game (Dockerfile.game)
├── Flask panel (port 8090)          └── 7DaysToDieServer.x86_64
│   ├── Docker SDK → start/stop            (managed by panel)
│   ├── tails /logs/server.log
│   └── SteamCMD (on demand)
```

Both containers share four bind-mount volumes: `/serverfiles`, `/config`, `/gamedata`, `/logs`.

The game container has `restart: "no"` — Docker never auto-restarts it. The panel controls its lifecycle. The panel has `restart: unless-stopped`.

## Directory Layout

```
/srv/7d2d-server/
├── Dockerfile               # Panel image (Flask + SteamCMD)
├── Dockerfile.game          # Game image (Ubuntu + lib32gcc + ca-certs + gosu)
├── docker-compose.yml
├── scripts/
│   ├── entrypoint.sh        # Panel: user/group setup, chmod docker.sock, exec gosu → Flask
│   └── game-entrypoint.sh   # Game: user/group setup, symlinks, cd serverfiles, exec game binary
├── panel/
│   ├── app.py               # All backend logic
│   ├── requirements.txt     # Flask + requests + docker
│   └── templates/
│       ├── _api_warning.html  # Shared banner partial — included by all pages
│       ├── dashboard.html     # Server controls + stats
│       ├── login.html
│       ├── config.html        # sdtdserver.xml + platform.cfg editor
│       ├── saves.html         # Save/world manager
│       ├── admin.html         # serveradmin.xml manager
│       ├── sandbox.html       # Visual SandboxCode editor
│       ├── console.html       # Live log stream + command input
│       ├── players.html       # Online players + actions + inventory viewer
│       ├── give.html          # Give items
│       └── teleport.html      # Waypoint browser + bot config
└── data/                    # Runtime data (gitignored)
    ├── serverfiles/         # Game installation (SteamCMD target) + platform.cfg
    ├── gamedata/            # Saves, worlds, serveradmin.xml
    ├── config/              # sdtdserver.xml, teleport_data.json
    └── logs/                # Game server log files
```

## Volume Mounts (container paths)

| Host | Container | Contents |
|---|---|---|
| `./data/serverfiles` | `/serverfiles` | Game binaries + platform.cfg |
| `./data/gamedata` | `/gamedata` | Saves, GeneratedWorlds, serveradmin.xml |
| `./data/config` | `/config` | sdtdserver.xml, teleport_data.json |
| `./data/logs` | `/logs` | Game server log files |

## Key Environment Variables

### Panel (`7dtd-panel`)

| Variable | Default | Purpose |
|---|---|---|
| `PUID` / `PGID` | `1000` | UID/GID the panel runs as |
| `PANEL_PASSWORD` | `admin` | Web panel login password |
| `SECRET_KEY` | random | Flask session signing key |
| `GAME_BRANCH` | `public` | `public` or `latest_experimental` |
| `GAME_API_URL` | `http://7dtd-game:8080` | Game REST API (Docker internal DNS) |
| `GAME_API_TOKEN_NAME` | — | Token name from serveradmin.xml |
| `GAME_API_SECRET` | — | Token secret from serveradmin.xml |
| `GAME_CONTAINER_NAME` | `7dtd-game` | Docker container name to manage |
| `CONFIG_PATH` | `/config/sdtdserver.xml` | Active server config |
| `ADMIN_PATH` | `/gamedata/Saves/serveradmin.xml` | Admin XML |
| `SAVES_ROOT` | `/gamedata/Saves` | Saves directory |
| `WORLDS_ROOT` | `/gamedata/GeneratedWorlds` | RWG worlds directory |
| `TELEPORT_DATA_PATH` | `/config/teleport_data.json` | Teleport bot data |
| `SERVERFILES_PATH` | `/serverfiles` | Game installation directory |
| `STEAMCMD_PATH` | `/opt/steamcmd/steamcmd.sh` | SteamCMD script |
| `LOG_DIR` | `/logs` | Directory for game server log files |

### Game (`7dtd-game`)

| Variable | Default | Purpose |
|---|---|---|
| `PUID` / `PGID` | `1000` | UID/GID the game process runs as |
| `SERVERFILES_PATH` | `/serverfiles` | Game installation directory |
| `CONFIG_PATH` | `/config/sdtdserver.xml` | Active server config |
| `GAMEDATA_PATH` | `/gamedata` | Game data directory |
| `LOG_DIR` | `/logs` | Log output directory |

## Privilege Drop (PUID/PGID)

Both containers run as root to start. Their respective entrypoint scripts create a group/user matching `PGID`/`PUID`, chown the volume dirs, then use `exec gosu ${PUID}:${PGID}` to drop privileges. The panel entrypoint also does `chmod 666 /var/run/docker.sock` so the service user can call the Docker API.

## Server Process Management (`app.py`)

### State machine
`_server_state` global: `stopped → starting → running → stopping → stopped`
Also: `installing` (SteamCMD running, blocks start/stop).

### Key globals
- `_docker_client` — `docker.DockerClient` from `/var/run/docker.sock`, `None` if socket unavailable
- `_server_state` — current state string, guarded by `_server_lock`
- `_log_buffer` — `deque(maxlen=500)` of log entry dicts
- `_log_subs` — list of `queue.Queue` objects, one per SSE client

### Server startup
```python
server_start()
  → _game_container().start()          # Docker SDK call
  → threading.Thread(_log_tail_reader) # starts tailing /logs/server.log
  → state: stopped → starting
```

### Log tail (`_log_tail_reader`)
Waits up to 60s for `server.log` to appear, then:
1. Seeks back ~50 KB from end and pre-populates `_log_buffer` with recent history
2. Tails new lines, pushing each to the buffer and all SSE subscriber queues
3. Drives state transitions (`starting → running` on `"Started Webserver on port"`)
4. Drives the chat teleport bot
5. Polls Docker every 3s of idle to detect if the game container exited

### Log rotation
`_rotate_server_log()` runs on each `server_start()`: renames `server.log` to `server-YYYY-MM-DD-HHMMSS.log` (using file mtime) if it exists and is non-empty.

### Startup sync
On panel startup, `_sync_container_state()` checks whether `7dtd-game` is already running (e.g. panel was rebuilt while game was live) and if so sets `_server_state = "running"` and starts `_log_tail_reader` immediately.

### SIGTERM handler
Calls `os._exit(0)` — the game container runs independently and is not stopped when the panel container stops.

### Branch detection
`_installed_branch()` reads `steamapps/appmanifest_294420.acf` and regex-searches for `"betakey"` using `re.IGNORECASE` — the manifest writes `"BetaKey"` with mixed case.

### Log format
Raw game output: `2026-01-01T00:00:00 123.456 INF message text`
Parsed by `_parse_raw()` into `{"msg": "...", "type": "Log|Warning|Error|Exception", "isotime": "..."}`.

### SSE log stream
`GET /api/log-stream` — sends buffered entries on connect (up to 500), then streams new entries as `event: logLine` with JSON data. The console template listens with `es.addEventListener('logLine', ...)`.

## Game API

The game runs its REST API on port 8080. Inside the panel container it's reachable at `http://7dtd-game:8080` (Docker internal DNS). Requires `X-SDTD-API-TOKENNAME` + `X-SDTD-API-SECRET` headers.

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

## Allocs Server Fixes (v3.0 build `30_38_52`)

Compatible with v3.0 b252+. Installed in `data/serverfiles/Mods/`:
- `Allocs_CommonFunc` — core library
- `Allocs_CommandExtensions` — adds `listknownplayers`, `showinventory`, `listlandprotection`, `removelandprotection` console commands
- `Allocs_WebAndMapRendering` — adds web API endpoints and map tile rendering

### Allocs API endpoints (port 8080)

| Endpoint | Purpose |
|---|---|
| `GET /api/GetPlayersOnline` | Online players with level, health, kills, playtime (richer than built-in `/api/player`) |
| `GET /api/GetPlayersLocation` | Player positions |
| `GET /api/GetPlayerInventories` | All online players' inventories |
| `GET /api/getplayerinventory?userid=Steam_XXX` | Single player inventory (bag, belt, equipment) |
| `GET /api/getplayerlist` | Player list with ban status, totalplaytime, lastonline |
| `GET /api/GetLandClaims` | Land claim positions |

### What Allocs v3.0 does NOT yet expose
- **Gamestage** and **lootstage** — not in any REST endpoint or console command. The v3.0 build is a stripped-down initial port; these were present in older Allocs builds and may return in a future revision.

### Panel inventory endpoint
The panel's `GET /api/inventory/<steam_id>` proxies to Allocs `getplayerinventory`. Allocs must be installed and the server running for this to work.

## Item Icons

The game's built-in `IconHandler` (served at `/itemicons/{name}__{tint}.png`) fails on headless servers because the item icon atlas is 8192×8192 but Unity's null GPU device caps textures at 4096×4096. The atlas loads as a dummy texture, so the handler has nothing to serve and logs `[Web] IconHandler: Icons not loaded` for every request.

**Fix:** `api_itemicon` in `app.py` serves icons directly from `SERVERFILES_PATH/Data/ItemIcons/{name}.png` (5161 PNG files shipped with the game). The game API is used as a fallback only. Tint color is not applied (base icon only), but icons display correctly.

## platform.cfg

The game reads `serverfiles/platform.cfg` on startup to determine the platform stack:

```
platform=Steam
crossplatform=EOS
serverplatforms=Steam,XBL,PSN,LAN,
```

- `crossplatform=EOS` — requires outbound HTTPS to `api.epicgames.dev` on every startup, even for private servers. Setting it to empty disables EOS; the server runs Steam+LAN only with no internet requirement.
- `crossplatform=` (empty) — XBL and PSN will error on init and be dropped from `serverplatforms` automatically (they require EOS). Steam and LAN continue normally.
- The game container image must include `ca-certificates` for the EOS TLS handshake to succeed.
- The game binary must run with `SERVERFILES_PATH` as its working directory (Unity looks for bundled native libs relative to CWD). `game-entrypoint.sh` does `cd "${SERVERFILES_PATH}"` before exec.

Config → Platform in the panel reads/writes `platform.cfg` directly. `crossplatform` and `serverplatforms` are routed to `platform.cfg`; all other config keys go to `sdtdserver.xml`.

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

Reads/writes `/config/sdtdserver.xml` (XML) and `/serverfiles/platform.cfg` (key=value). Both are surfaced in the same Config page across sidebar sections.

`_PLATFORM_CFG_KEYS = {"crossplatform", "serverplatforms"}` — keys in this set are routed to `platform.cfg` by `api_config_save()`; all others go to `sdtdserver.xml`.

Settings organized via `_SECTIONS` (list of dicts with `id`, `label`, `icon`, `fields`). Field rendering driven by:
- `_FIELD_TYPES` — `text | number | boolean | select | sandbox_code`
- `_FIELD_META` — per-field `label`, `options`, `fix_when_false`, `fix_when_not`
- `_FIELD_DESCRIPTIONS` — optional helper text shown below the input

`GameWorld.options` populated dynamically by `_available_worlds()` (scans `SERVERFILES_PATH/Data/Worlds/`).

### Config API
- `GET /api/config` — returns merged flat JSON dict from both `sdtdserver.xml` and `platform.cfg`
- `POST /api/config` — accepts `{"updates": {"Key": "value", ...}}`, routes to the correct file

## Sandbox Editor

Full visual editor for v3.0 SandboxCode. 150+ options across 8 categories (Player, Entities, World, Resources, Crafting, Tasks, Traders, Misc).

### Encode/decode
- `encode()` — builds code string from non-default state entries: `A` prefix + 3-char triplets `[enumId_hi][enumId_lo][valueIdx]` (all base-26, A=0)
- `decode(code)` — parses triplets back into state indices

## Admin Manager

Full CRUD for `serveradmin.xml` with five sections: `users`, `whitelist`, `blacklist`, `commands`, `apitokens`.

The `/admin` route must pass **both** `data=parse_admin()` and `tele_data=_load_tele()` to the template.

Routes:
- `GET /api/admin` — returns all sections as JSON
- `POST /api/admin/<section>` — append entry
- `DELETE /api/admin/<section>` — remove by criteria (JSON body with matching key/value pairs)

Permission levels: `0` = full admin, `1000` = default player.

## Deploying Changes

Rebuild only the panel — **game server stays running**:

```bash
git pull && docker compose up -d --build 7dtd-panel
```

To rebuild both (game server will stop and must be restarted from Dashboard):

```bash
git pull && docker compose up -d --build
```

## v3.0 API Notes

- SSE endpoint: `/sse/?events=log` with `events` query param (not `/sse/log`)
- SSE event name: `logLine` (not the default `message`)
- Allocs `30_38_52` is compatible with v3.0 b252+ and installed — see Allocs section above
- Loot stage / game stage not exposed by v3.0 built-in API or current Allocs build
- `appmanifest_294420.acf` uses `"BetaKey"` (mixed case) — always use `re.IGNORECASE` when parsing it
