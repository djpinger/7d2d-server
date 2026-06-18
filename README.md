# 7 Days to Die — Self-Hosted Server + Web Panel

A single Docker container that runs both the 7 Days to Die dedicated server and a web-based admin panel. The panel manages the game server as a child process, so you can start, stop, restart, and update the server from the browser without touching Docker.

## Features

### Dashboard
- Server state badge (stopped / starting / running / stopping / installing) with live polling
- One-click **Start / Stop / Restart**
- **Install/Update** via SteamCMD — auto-detects installed branch and pre-selects the dropdown
- **Verify Files** — re-validates game files against Steam without a full reinstall
- Live stats: player count, game day, server FPS, heap memory

### Config Editor
Settings are organized into a sidebar with sections: Server, Network, Gameplay, Zombies, Sandbox, Performance, Mods.
- **GameWorld** dropdown is populated dynamically from the installed server's `Data/Worlds/` directory (picks up new maps after game updates automatically)
- **WorldGenSize** is a dropdown of the three officially supported RWG sizes (6144 / 8192 / 10240)
- **SandboxCode** field has a *Reload Saved* button; description points to the Sandbox tab for full editing
- **WebDashboardEnabled** is highlighted in red with a Fix button when disabled — the game's REST API (port 8080) must be on for players, commands, stats, and Give Items to work

### API Warning Banner
Every page shows an amber banner directly below the navbar when `WebDashboardEnabled` is off in the config. The banner has a **Fix Now** button that enables it in place and a **×** to dismiss for the session. The field itself in Config → Network also has a Fix button.

### Sandbox Editor
Full visual editor for the v3.0 SandboxCode — 150+ individual settings across 8 categories (Player, Entities, World, Resources, Crafting, Tasks, Traders, Misc). Non-default settings are highlighted; per-option reset buttons revert individual values.
- **Apply to Config** — writes the generated code directly to `sdtdserver.xml`
- **Load from Config** — decodes the saved config code into the editor
- **Reset to Defaults** — resets all options to game defaults
- **Decode** — paste any SandboxCode string to decode it into the editor
- Opens with your saved settings already loaded from config

### Console
Live log stream from the game server process (SSE). Log lines color-coded by severity. Command input bar sends commands via the game API.

### Players
Online player list (polled every 30s). Per-player actions: PM, Give Items (links to Give page), Kick, Ban, Grant/Revoke Admin.

### Give Items
Full item catalogue (~10k non-block items) loaded from the game API on page load, filtered client-side. Shows localized name + internal ID with quality and quantity selectors.

### Teleport
Admin view of the in-game chat bot. Browse all saved waypoints, delete any entry, teleport any player to arbitrary coordinates. Configurable cooldown, daily limit, and per-player waypoint cap.

**In-game chat commands:**
| Command | Effect |
|---|---|
| `!settele <name>` | Save current position as a named waypoint |
| `!tele <name>` | Teleport to a saved waypoint |
| `!deltele <name>` | Delete a waypoint |
| `!listtele` | List your waypoints |

### Saves
Browse all worlds and saves with size, player count, and last-modified date. Wipe gameplay state (keeps terrain), delete a save, or delete a generated world entirely.

### Admin
Full CRUD for `serveradmin.xml` across five tabs: Users, Whitelist, Blacklist, Commands, API Tokens.

---

## Quick Start

### 1. Clone and configure

```bash
git clone https://github.com/djpinger/7d2d-server.git
cd 7d2d-server
```

Edit `docker-compose.yml` and set at minimum:

| Variable | Description |
|---|---|
| `PUID` | UID to run the server process as (default `1000`) |
| `PGID` | GID to run the server process as (default `1000`) |
| `PANEL_PASSWORD` | Web panel login password |
| `SECRET_KEY` | Flask session key — use a long random string |
| `GAME_API_TOKEN_NAME` | Token name added to Admin → API Tokens |
| `GAME_API_SECRET` | Matching secret for the token above |
| `GAME_BRANCH` | `public` (stable) or `latest_experimental` |

### 2. Start the panel

```bash
docker compose up -d
```

The panel is available at `http://<your-ip>:8090`. The game server is **not** running yet — install it first.

### 3. Install the game server

1. Open the panel → **Dashboard**
2. Select your branch (`public` or `latest_experimental`)
3. Click **Install/Update**
4. Watch progress on **Console** — takes 5–10 minutes on first install

### 4. Add an API token

Before starting the server, add an API token so the panel can query player data and run commands:

1. Go to **Admin → API Tokens → Add Token**
2. Set the name and secret to match `GAME_API_TOKEN_NAME` / `GAME_API_SECRET` in `docker-compose.yml`
3. Set `permission_level` to `0`

The token lives in `data/gamedata/Saves/serveradmin.xml` and persists across restarts.

### 5. Start the server

Dashboard → **Start**. First launch generates the world (can take several minutes). The server is ready when the Console shows `Started Webserver on port 8080`.

---

## Directory Layout

```
7d2d-server/
├── Dockerfile
├── docker-compose.yml
├── scripts/
│   └── entrypoint.sh          # privilege drop via gosu, then exec Flask
├── panel/
│   ├── app.py                 # all backend logic
│   ├── requirements.txt
│   └── templates/
│       ├── dashboard.html
│       ├── console.html
│       ├── players.html
│       ├── give.html
│       ├── teleport.html
│       ├── config.html
│       ├── saves.html
│       ├── admin.html
│       ├── sandbox.html
│       └── login.html
└── data/                      ← created on first run, not committed to git
    ├── serverfiles/           ← game installation (SteamCMD target)
    ├── gamedata/              ← saves, worlds, serveradmin.xml
    ├── config/                ← sdtdserver.xml, teleport_data.json
    └── logs/                  ← game server log files
```

## Ports

| Port | Purpose |
|---|---|
| `8090` | Web panel |
| `26900 TCP+UDP` | Game (clients connect here) |
| `26901 UDP` | Game |
| `26902 UDP` | Game |
| `18080` | Game built-in web API (optional external access) |

## User / Permission Model

The container entrypoint runs as root, creates the service user at the requested `PUID`/`PGID`, chowns the data directories, then uses `gosu` to drop to that user before exec-ing Flask. All game files and saves are written as `PUID:PGID` on the host. Set these to match your host user to avoid permission issues with the `data/` bind mounts.

## Deploying Panel Changes

Panel code is baked into the image. After editing templates or `app.py`:

```bash
git pull
docker compose up -d --build
```

The game server process is killed when the container restarts. Start it again from the Dashboard after the rebuild.

## Log Files

Game server output is written to `./data/logs/server.log` in real time. Each time the server starts, the previous log is archived as `server-YYYY-MM-DD-HHMMSS.log` so you retain a history of past sessions.

```bash
# Follow the current session live from the host
tail -f ./data/logs/server.log

# List archived sessions
ls -lh ./data/logs/
```

`docker logs 7d2d` shows only Flask/panel output (HTTP requests, install progress). Game server output goes to `./data/logs/server.log` only.

## Updating the Game Server

Dashboard → **Install/Update** → select branch → click the button. The panel will refuse to run the update if the game server is currently running — stop it first. Use **Verify Files** to repair a running installation without a full re-download.

## License

MIT
