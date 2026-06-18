# 7 Days to Die — Self-Hosted Server + Web Panel

A single Docker container that runs both the 7 Days to Die dedicated server and a web-based admin panel. The panel manages the game server as a child process, so you can start, stop, restart, and update the server from the browser without touching Docker.

## Features

- **Dashboard** — server status, one-click start/stop/restart, install/update via SteamCMD
- **Console** — live log stream (from process stdout) + command input
- **Players** — online player list with PM, Give, Kick, Ban, and permission controls
- **Give Items** — searchable item catalogue with quality/quantity selector
- **Teleport** — admin waypoint browser + manual teleport; in-game chat bot (`!settele`, `!tele`, etc.)
- **Config** — edit `sdtdserver.xml` in the browser
- **Saves** — browse worlds/saves, wipe or delete them
- **Admin** — manage `serveradmin.xml` (users, whitelist, blacklist, commands, API tokens)
- **Sandbox** — paste/edit the v3.0 SandboxCode string

## Quick Start

### 1. Clone and configure

```bash
git clone https://github.com/djpinger/7d2d-server.git
cd 7d2d-server
```

Edit `docker-compose.yml` and set at minimum:

| Variable | Description |
|---|---|
| `PANEL_PASSWORD` | Web panel login password |
| `SECRET_KEY` | Flask session key — use a long random string |
| `GAME_API_TOKEN_NAME` | Token name added to `serveradmin.xml` → API Tokens |
| `GAME_API_SECRET` | Matching secret for the token above |
| `GAME_BRANCH` | `public` (stable) or `latest_experimental` |

### 2. Start the panel

```bash
docker compose up -d
```

The panel is available at `http://<your-ip>:8090`. The game server is **not** running yet — you need to install it first.

### 3. Install the game server

1. Open the panel → **Dashboard**
2. Select your branch (public or latest_experimental)
3. Click **Install/Update**
4. Watch progress on the **Console** page — takes 5–10 minutes on first install

### 4. Add an API token

Before starting the server, add an API token so the panel can query player data and run commands:

1. Go to **Admin → API Tokens → Add Token**
2. Set the name and secret to match `GAME_API_TOKEN_NAME` / `GAME_API_SECRET` in `docker-compose.yml`
3. Set `permission_level` to `0`

The token lives in `data/gamedata/Saves/serveradmin.xml` and persists across restarts.

### 5. Start the server

Dashboard → **Start**. First launch generates the world (can take several minutes). Watch the Console page — the server is ready when you see `Started Webserver on port 8080`.

## Directory Layout

```
7d2d-server/
├── Dockerfile
├── docker-compose.yml
├── scripts/
│   └── entrypoint.sh
├── panel/
│   ├── app.py
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
└── data/                        ← created on first run, not committed
    ├── serverfiles/             ← game installation (SteamCMD target)
    ├── gamedata/                ← saves, worlds, serveradmin.xml
    └── config/                  ← sdtdserver.xml, teleport_data.json
```

## Ports

| Port | Purpose |
|---|---|
| `8090` | Web panel |
| `26900 TCP+UDP` | Game (clients connect here) |
| `26901 UDP` | Game |
| `26902 UDP` | Game |
| `18080` | Game built-in web API (optional external access) |

## Teleport Bot

Players use chat commands in-game:

| Command | Effect |
|---|---|
| `!settele <name>` | Save current position as a waypoint |
| `!tele <name>` | Teleport to a saved waypoint |
| `!deltele <name>` | Delete a waypoint |
| `!listtele` | List your waypoints |

Configure cooldown, daily limit, and max waypoints per player on the **Teleport** page.

## Updating the Panel

Changes to panel code require a rebuild:

```bash
git pull
docker compose up -d --build
```

The game server does **not** restart during a panel rebuild — only the container process restarts (which relaunches Flask). If the game server was running, you'll need to start it again from the Dashboard after the rebuild.

## Updating the Game Server

Dashboard → **Install/Update** → select branch → click button. The panel will refuse to run the update if the game server is currently running — stop it first.

## License

MIT
