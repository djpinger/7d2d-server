# 7 Days to Die — Self-Hosted Server + Web Panel

Two Docker containers: a web-based admin panel and the game server. The panel manages the game server's lifecycle via the Docker API — you can start, stop, restart, and update without touching Docker, and **rebuilding the panel does not affect a running game server**.

## Features

### Dashboard
- Server state badge (stopped / starting / running / stopping / installing) with live polling
- One-click **Start / Stop / Restart**
- **Install/Update** via SteamCMD — auto-detects installed branch and pre-selects the dropdown
- **Verify Files** — re-validates game files against Steam without a full reinstall
- Live stats: player count, game day, server FPS, heap memory

### Config Editor
Settings organized into sidebar sections: Server, Network, Platform, Gameplay, Zombies, Sandbox, Performance, Mods.
- **Platform section** edits `platform.cfg` directly — toggle EOS cross-platform on/off and set allowed platforms
- **GameWorld** dropdown populated dynamically from the installed server's `Data/Worlds/` directory
- **WorldGenSize** dropdown of the three officially supported RWG sizes (6144 / 8192 / 10240)
- **WebDashboardEnabled** highlighted in red with a Fix button when disabled — required for the game API

### API Warning Banner
Amber banner on every page when `WebDashboardEnabled` is off. **Fix Now** button enables it in place.

### Sandbox Editor
Full visual editor for the v3.0 SandboxCode — 150+ individual settings across 8 categories. Non-default settings highlighted; per-option reset buttons. Apply to Config, Load from Config, Reset to Defaults, Decode any code string.

### Console
Live log stream (SSE) with recent history pre-loaded on connect. Color-coded by severity. Command input bar sends commands via the game API.

### Players
Online player list (polled every 30s). Per-player actions: PM, Give Items, Kick, Ban. Expandable inventory view per player.

### Give Items
Full item catalogue (~10k non-block items) loaded from the game API, filtered client-side. Quality and quantity selectors.

### Teleport
Admin view of the in-game chat bot. Browse all waypoints, delete any entry, teleport any player to arbitrary coordinates. Configurable cooldown, daily limit, and per-player waypoint cap.

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
| `PUID` / `PGID` | UID/GID to run as (default `1000`) |
| `PANEL_PASSWORD` | Web panel login password |
| `SECRET_KEY` | Flask session key — use a long random string |
| `GAME_API_TOKEN_NAME` | Token name you'll add in Admin → API Tokens |
| `GAME_API_SECRET` | Matching secret for that token |
| `GAME_BRANCH` | `public` (stable) or `latest_experimental` |

### 2. Start the panel

```bash
docker compose up -d 7dtd-panel
```

The panel starts at `http://<your-ip>:8090`. The game container is built but not started — install the game first.

### 3. Install the game server

1. Open the panel → **Dashboard**
2. Select your branch
3. Click **Install/Update**
4. Watch progress in **Console** — takes 5–10 minutes on first install

### 4. Add an API token

Before starting the server, add an API token so the panel can query player data and run commands:

1. Go to **Admin → API Tokens → Add Token**
2. Set name and secret to match `GAME_API_TOKEN_NAME` / `GAME_API_SECRET` in `docker-compose.yml`
3. Set `permission_level` to `0`

The token lives in `data/gamedata/Saves/serveradmin.xml` and persists across restarts.

### 5. Start the server

Dashboard → **Start**. First launch generates the world (several minutes). Ready when Console shows `Started Webserver on port 8080`.

---

## Directory Layout

```
7d2d-server/
├── Dockerfile               # Panel image (Flask + SteamCMD)
├── Dockerfile.game          # Game image (Ubuntu minimal + ca-certificates + gosu)
├── docker-compose.yml
├── scripts/
│   ├── entrypoint.sh        # Panel entrypoint: privilege drop → Flask
│   └── game-entrypoint.sh   # Game entrypoint: privilege drop → 7DaysToDieServer
├── panel/
│   ├── app.py
│   ├── requirements.txt
│   └── templates/
└── data/                    ← created on first run, not committed to git
    ├── serverfiles/         ← game installation + platform.cfg
    ├── gamedata/            ← saves, worlds, serveradmin.xml
    ├── config/              ← sdtdserver.xml, teleport_data.json
    └── logs/                ← game server log files
```

## Ports

| Port | Purpose |
|---|---|
| `8090` | Web panel |
| `26900 TCP+UDP` | Game (clients connect here) |
| `26901 UDP` | Game |
| `26902 UDP` | Game |
| `18080` | Game built-in web API (optional external access) |

## EOS / Cross-Platform

7 Days to Die v3.0 uses Epic Online Services (EOS) as its platform layer. By default this requires outbound HTTPS to `api.epicgames.dev` on every server startup, even for private Steam-only servers.

To disable it: go to **Config → Platform** and set **Cross-Platform Backend** to *Disabled*. This writes `crossplatform=` (empty) to `data/serverfiles/platform.cfg`. The server will start without any internet requirement; XBL and PSN platforms are dropped automatically (they require EOS). Steam and LAN continue normally.

## Deploying Panel Changes

Rebuild only the panel — **game server keeps running**:

```bash
git pull && docker compose up -d --build 7dtd-panel
```

To rebuild everything (game server will stop and must be restarted from Dashboard):

```bash
git pull && docker compose up -d --build
```

## Log Files

Game server output is written to `./data/logs/server.log`. Each time the server starts, the previous log is archived as `server-YYYY-MM-DD-HHMMSS.log`.

```bash
tail -f ./data/logs/server.log
```

`docker logs 7dtd-panel` shows only Flask/panel output. `docker logs 7dtd-game` shows the game container's early startup (before the log file is established).

## Updating the Game Server

Dashboard → **Install/Update** → select branch → click the button. Stop the server first — the panel will refuse to run the update while it's running. Use **Verify Files** to repair an installation without a full re-download.

## License

MIT
