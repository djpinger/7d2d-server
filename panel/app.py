import io
import os
import re
import json
import queue
import shutil
import secrets
import signal
import subprocess
import tarfile
import threading
import time
import collections
import zipfile
from datetime import date, datetime
from functools import wraps
from pathlib import Path
from xml.etree import ElementTree as ET

from flask import (Flask, Response, jsonify, redirect, render_template,
                   request, send_file, session, stream_with_context, url_for)
import requests as _req

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

PANEL_PASSWORD     = os.environ.get("PANEL_PASSWORD",      "admin")
CONFIG_PATH        = Path(os.environ.get("CONFIG_PATH",    "/config/sdtdserver.xml"))
ADMIN_PATH         = Path(os.environ.get("ADMIN_PATH",     "/gamedata/Saves/serveradmin.xml"))
SAVES_ROOT         = Path(os.environ.get("SAVES_ROOT",     "/gamedata/Saves"))
WORLDS_ROOT        = Path(os.environ.get("WORLDS_ROOT",    "/gamedata/GeneratedWorlds"))
GAMEDATA_PATH      = Path(os.environ.get("GAMEDATA_PATH",  "/gamedata"))
ALLOCS_URL         = "https://illy.bz/fi/7dtd/server_fixes.tar.gz"
ALLOCS_MODS        = ["Allocs_CommonFunc", "Allocs_CommandExtensions", "Allocs_WebAndMapRendering"]
MODS_PACK_CONFIG_PATH = Path(os.environ.get("MODS_PACK_CONFIG_PATH", "/config/mods_pack_config.json"))
MODS_PACK_PATH        = Path(os.environ.get("MODS_PACK_PATH",        "/config/mods.zip"))
# Server-only mods (web/console tooling) that ship no client-relevant content,
# excluded from the client pack by default unless the admin opts them back in.
_DEFAULT_EXCLUDED_MODS = {"Allocs_CommonFunc", "Allocs_CommandExtensions",
                          "Allocs_WebAndMapRendering", "TFP_CommandExtensions"}
TELEPORT_DATA_PATH = Path(os.environ.get("TELEPORT_DATA_PATH", "/config/teleport_data.json"))
SERVERFILES_PATH   = Path(os.environ.get("SERVERFILES_PATH",   "/serverfiles"))
PLATFORM_CFG_PATH  = SERVERFILES_PATH / "platform.cfg"
STEAMCMD           = Path(os.environ.get("STEAMCMD_PATH",       "/opt/steamcmd/steamcmd.sh"))
LOG_DIR            = Path(os.environ.get("LOG_DIR",             "/logs"))
GAME_BRANCH          = os.environ.get("GAME_BRANCH",         "public")
GAME_API_URL         = os.environ.get("GAME_API_URL",        "http://7dtd-game:8080")
GAME_API_TOKEN       = os.environ.get("GAME_API_TOKEN_NAME", "")
GAME_API_SECRET      = os.environ.get("GAME_API_SECRET",     "")
GAME_CONTAINER_NAME  = os.environ.get("GAME_CONTAINER_NAME", "7dtd-game")

import docker as _docker_module
try:
    _docker_client = _docker_module.from_env()
except Exception as _docker_err:
    print(f"[panel] WARNING: Docker socket unavailable: {_docker_err}", flush=True)
    _docker_client = None


# ─── Log buffer + SSE ─────────────────────────────────────────────────────────

_log_buffer   = collections.deque(maxlen=500)
_log_subs     = []
_log_subs_lock = threading.Lock()

def _log_push(msg: str, typ: str = "Log", source: str = "server"):
    entry = {"msg": msg, "type": typ, "isotime": datetime.utcnow().isoformat() + "Z", "source": source}
    _log_buffer.append(entry)
    with _log_subs_lock:
        dead = []
        for q in _log_subs:
            try:
                q.put_nowait(entry)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _log_subs.remove(q)

_LOG_LINE_RE = re.compile(r"^\S+T\S+ \S+ (INF|WRN|ERR|EXC) (.+)$")
_CHAT_RE     = re.compile(r"Chat \(from '([^']+)', entity id '(\d+)', to '([^']*)'\): '([^']*)': (.*)")

def _parse_raw(raw: str):
    m = _LOG_LINE_RE.match(raw)
    if m:
        level, msg = m.group(1), m.group(2)
        typ = {"INF": "Log", "WRN": "Warning", "ERR": "Error", "EXC": "Exception"}.get(level, "Log")
        return msg, typ
    return raw, "Log"


# ─── Server log file ──────────────────────────────────────────────────────────

def _rotate_server_log():
    """Archive previous server.log to a dated name; game container writes the new one."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    current = LOG_DIR / "server.log"
    if current.exists() and current.stat().st_size > 0:
        ts = datetime.fromtimestamp(current.stat().st_mtime).strftime("%Y-%m-%d-%H%M%S")
        current.rename(LOG_DIR / f"server-{ts}.log")


# ─── Server process management ────────────────────────────────────────────────

_server_state = "stopped"   # stopped | starting | running | stopping | installing
_server_lock  = threading.Lock()


def _log_tail_reader():
    """Background thread: tails server.log from the shared volume, drives log + chat bot."""
    global _server_state
    log_path = LOG_DIR / "server.log"

    # Wait for the game container to create the log file (up to 60 s)
    deadline = time.monotonic() + 60
    while not log_path.exists():
        if _server_state not in ("starting", "running"):
            return
        if time.monotonic() > deadline:
            _log_push("Timed out waiting for server.log to appear.", "Warning", "panel")
            with _server_lock:
                if _server_state not in ("stopping", "stopped", "installing"):
                    _server_state = "stopped"
            return
        time.sleep(0.5)

    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            # Pre-populate buffer with recent history (~50 KB back from end)
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 51200))
            if size > 51200:
                f.readline()  # skip the partial first line
            for _raw in f.readlines():
                _raw = _raw.rstrip()
                if _raw:
                    _msg, _typ = _parse_raw(_raw)
                    _log_push(_msg, _typ)
            # now tail from current position (end of pre-populated content)
            _last_check = time.monotonic()

            while True:
                line = f.readline()
                if line:
                    _last_check = time.monotonic()
                    raw = line.rstrip()
                    if not raw:
                        continue
                    msg, typ = _parse_raw(raw)
                    _log_push(msg, typ)

                    if _server_state == "starting" and "Started Webserver on port" in msg:
                        with _server_lock:
                            if _server_state == "starting":
                                _server_state = "running"

                    if _server_state == "starting" and "StartGame done" in msg:
                        with _server_lock:
                            if _server_state == "starting":
                                _server_state = "running"

                    if "Webserver not started, WebDashboardEnabled set to false" in msg:
                        _log_push(
                            "WARNING: WebDashboardEnabled is false — the panel cannot reach the game API. "
                            "Go to Config → Network, enable Web Dashboard, set port to 8080, then restart.",
                            "Error", "panel"
                        )

                    cm = _CHAT_RE.search(msg)
                    if cm:
                        threading.Thread(
                            target=_handle_chat,
                            args=(cm.group(1), int(cm.group(2)), cm.group(4), cm.group(5)),
                            daemon=True,
                        ).start()
                else:
                    time.sleep(0.1)
                    if _server_state == "stopping":
                        break
                    # Every 3 s verify the game container is still running
                    if time.monotonic() - _last_check > 3.0:
                        _last_check = time.monotonic()
                        try:
                            c = _docker_client.containers.get(GAME_CONTAINER_NAME)
                            if c.status not in ("running", "restarting"):
                                break
                        except Exception:
                            break
    except Exception:
        pass
    finally:
        with _server_lock:
            if _server_state not in ("stopping", "stopped", "installing"):
                _server_state = "stopped"
        _log_push("Server process exited.", "Warning", "panel")


def _game_container():
    if _docker_client is None:
        raise RuntimeError("Docker socket unavailable — is /var/run/docker.sock mounted?")
    return _docker_client.containers.get(GAME_CONTAINER_NAME)


def server_start():
    global _server_state
    with _server_lock:
        if _server_state in ("running", "starting"):
            return {"error": "Server already running"}
        if _server_state == "installing":
            return {"error": "Installation in progress"}
        if not (SERVERFILES_PATH / "7DaysToDieServer.x86_64").exists():
            return {"error": "Server not installed — use Install/Update first"}
        _rotate_server_log()
        _server_state = "starting"

    _log_push("Starting server…", source="panel")
    try:
        _game_container().start()
        threading.Thread(target=_log_tail_reader, daemon=True).start()
        return {"ok": True}
    except _docker_module.errors.NotFound:
        with _server_lock:
            _server_state = "stopped"
        return {"error": f"Game container '{GAME_CONTAINER_NAME}' not found — run: docker compose up -d"}
    except Exception as e:
        with _server_lock:
            _server_state = "stopped"
        return {"error": str(e)}


def server_stop():
    global _server_state
    with _server_lock:
        if _server_state in ("stopped", "stopping"):
            _server_state = "stopped"
            return {"error": "Server not running"}
        _server_state = "stopping"

    _log_push("Stopping server…", source="panel")
    try:
        _game_container().stop(timeout=60)
    except _docker_module.errors.NotFound:
        pass
    except Exception as e:
        _log_push(f"Stop error: {e}", "Error", "panel")

    with _server_lock:
        _server_state = "stopped"
    with _player_cache_lock:
        _player_cache.clear()
    _log_push("Server stopped.", source="panel")
    return {"ok": True}


def server_restart():
    r = server_stop()
    if "error" in r and r["error"] != "Server not running":
        return r
    time.sleep(2)
    return server_start()


def server_install(branch=None):
    global _server_state
    branch = branch or GAME_BRANCH
    with _server_lock:
        if _server_state not in ("stopped",):
            if _server_state == "installing":
                return {"error": "Installation already in progress"}
            return {"error": "Stop the server before installing"}
        _server_state = "installing"

    def _do():
        global _server_state
        SERVERFILES_PATH.mkdir(parents=True, exist_ok=True)
        args = [str(STEAMCMD),
                "+@sSteamCmdForcePlatformType", "linux",
                "+force_install_dir", str(SERVERFILES_PATH),
                "+login", "anonymous",
                "+app_update", "294420"]
        if branch != "public":
            args += ["-beta", branch]
        args += ["validate", "+quit"]

        _log_push(f"Installing 7D2D ({branch} branch)…", source="panel")
        try:
            p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, bufsize=1)
            for line in iter(p.stdout.readline, ""):
                line = line.rstrip()
                if line:
                    _log_push(line, source="steamcmd")
            p.wait()
            if p.returncode == 0:
                _log_push("Installation complete!", source="panel")
                _ensure_default_config()
                _write_installed_branch(branch)
            else:
                _log_push(f"Installation failed (exit {p.returncode})", "Error", "panel")
        except Exception as e:
            _log_push(f"Installation error: {e}", "Error", "panel")
        finally:
            with _server_lock:
                _server_state = "stopped"

    threading.Thread(target=_do, daemon=True).start()
    return {"ok": True}


def _ensure_default_config():
    if not CONFIG_PATH.exists():
        src = SERVERFILES_PATH / "serverconfig.xml"
        if src.exists():
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(CONFIG_PATH))
            _log_push(f"Default config written to {CONFIG_PATH}", source="panel")


def _server_installed():
    return (SERVERFILES_PATH / "7DaysToDieServer.x86_64").exists()


_BRANCH_FILE = SERVERFILES_PATH / "steamapps" / ".installed_branch"

def _write_installed_branch(branch: str):
    try:
        _BRANCH_FILE.write_text(branch)
    except Exception:
        pass

def _installed_branch() -> str:
    """Return the installed branch. Prefers our sidecar file (written after each
    successful install) because SteamCMD doesn't always clear BetaKey when
    switching back to public."""
    if _BRANCH_FILE.exists():
        try:
            b = _BRANCH_FILE.read_text().strip()
            if b:
                return b
        except Exception:
            pass
    manifest = SERVERFILES_PATH / "steamapps" / "appmanifest_294420.acf"
    if not manifest.exists():
        return GAME_BRANCH
    try:
        text = manifest.read_text()
        m = re.search(r'"betakey"\s+"([^"]*)"', text, re.IGNORECASE)
        if m and m.group(1):
            return m.group(1)
    except Exception:
        pass
    return "public"


# ─── Player cache ─────────────────────────────────────────────────────────────

_player_cache      = {}
_player_cache_lock = threading.Lock()


def _update_player_cache():
    resp = game_api_get("/api/player")
    players = resp.get("data", {}).get("players", [])
    if not isinstance(players, list):
        return
    cache = {}
    for p in players:
        uid      = p.get("platformId", {})
        steam_id = uid.get("combinedString") if isinstance(uid, dict) else None
        if not steam_id:
            continue
        pos = p.get("position", {})
        cache[steam_id] = {
            "entityId": p.get("entityId"),
            "name":     p.get("name", ""),
            "x": int(pos.get("x", 0)),
            "y": int(pos.get("y", 0)),
            "z": int(pos.get("z", 0)),
        }
    with _player_cache_lock:
        _player_cache.clear()
        _player_cache.update(cache)

    if cache:
        with _teleport_lock:
            tele = _load_tele()
            changed = False
            known = tele.setdefault("player_names", {})
            for sid, info in cache.items():
                if sid in tele.get("waypoints", {}) and sid not in known and info.get("name"):
                    known[sid] = info["name"]
                    changed = True
            if changed:
                _save_tele(tele)


def _player_by_name(name: str):
    with _player_cache_lock:
        for sid, info in _player_cache.items():
            if info["name"] == name:
                return sid, info
    return None, None


def _player_poller():
    while True:
        if _server_state == "running":
            try:
                _update_player_cache()
            except Exception:
                pass
        time.sleep(15)


# ─── Teleport / chat bot ──────────────────────────────────────────────────────

_teleport_lock = threading.Lock()
_TELEPORT_DEFAULTS = {
    "config": {"cooldown_seconds": 60, "daily_limit": 10, "max_waypoints_per_player": 5},
    "waypoints": {},
    "usage": {},
    "player_names": {},
}


def _load_tele() -> dict:
    if TELEPORT_DATA_PATH.exists():
        try:
            with open(TELEPORT_DATA_PATH) as f:
                data = json.load(f)
            for k, v in _TELEPORT_DEFAULTS.items():
                if k not in data:
                    data[k] = v if k != "config" else dict(v)
            for k, v in _TELEPORT_DEFAULTS["config"].items():
                data["config"].setdefault(k, v)
            return data
        except Exception:
            pass
    import copy
    return copy.deepcopy(_TELEPORT_DEFAULTS)


def _save_tele(data: dict):
    TELEPORT_DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(TELEPORT_DATA_PATH, "w") as f:
        json.dump(data, f, indent=2)


def _say(entity_id: int, msg: str):
    game_api_post("/api/command", {"command": f'sayplayer {entity_id} "{msg}"'})


def _teleport(entity_id: int, x, y, z):
    game_api_post("/api/command", {"command": f"teleportplayer {entity_id} {x} {y} {z}"})


def _handle_chat(steam_id: str, entity_id: int, player_name: str, message: str):
    msg  = message.strip()
    if not msg.startswith("!"):
        return
    parts = msg.split(None, 1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    with _teleport_lock:
        data         = _load_tele()
        cfg          = data["config"]
        waypoints    = data.setdefault("waypoints", {})
        usage        = data.setdefault("usage", {})
        player_wps   = waypoints.setdefault(steam_id, {})
        player_usage = usage.setdefault(steam_id, {"last_teleport": None, "teleports_today": 0, "day": None})

        today = date.today().isoformat()
        if player_usage.get("day") != today:
            player_usage["teleports_today"] = 0
            player_usage["day"] = today

        if player_name:
            data.setdefault("player_names", {})[steam_id] = player_name

        if cmd == "!settele":
            if not arg:
                _say(entity_id, "Usage: !settele [name]")
                return
            name = arg.split()[0].lower()
            if len(player_wps) >= cfg["max_waypoints_per_player"] and name not in player_wps:
                _say(entity_id, f"Max waypoints reached ({cfg['max_waypoints_per_player']}). Delete one first.")
                return
            with _player_cache_lock:
                info = _player_cache.get(steam_id)
            if not info:
                _say(entity_id, "Could not get your position. Try again in a moment.")
                return
            player_wps[name] = {"x": info["x"], "y": info["y"], "z": info["z"]}
            _save_tele(data)
            _say(entity_id, f"Teleport {name} created!")

        elif cmd == "!tele":
            if not arg:
                _say(entity_id, "Usage: !tele [name]")
                return
            name = arg.split()[0].lower()
            if name not in player_wps:
                _say(entity_id, f"No teleport named {name}. Use !listtele to see yours.")
                return
            now  = time.time()
            last = player_usage.get("last_teleport")
            if last:
                elapsed = now - last
                if elapsed < cfg["cooldown_seconds"]:
                    remaining = int(cfg["cooldown_seconds"] - elapsed)
                    _say(entity_id, f"Teleport on cooldown. {remaining}s remaining.")
                    return
            if cfg["daily_limit"] > 0 and player_usage["teleports_today"] >= cfg["daily_limit"]:
                _say(entity_id, f"Daily teleport limit of {cfg['daily_limit']} reached.")
                return
            wp = player_wps[name]
            _teleport(entity_id, wp["x"], wp["y"], wp["z"])
            player_usage["last_teleport"]  = now
            player_usage["teleports_today"] += 1
            _save_tele(data)
            _say(entity_id, f"Teleporting to {name}...")

        elif cmd == "!deltele":
            if not arg:
                _say(entity_id, "Usage: !deltele [name]")
                return
            name = arg.split()[0].lower()
            if name not in player_wps:
                _say(entity_id, f"No teleport named {name}.")
                return
            del player_wps[name]
            _save_tele(data)
            _say(entity_id, f"Teleport {name} deleted.")

        elif cmd == "!listtele":
            _save_tele(data)
            if not player_wps:
                _say(entity_id, "You have no saved teleports.")
            else:
                names = ", ".join(sorted(player_wps.keys()))
                _say(entity_id, f"Your teleports: {names}")


# ─── Game API helpers ─────────────────────────────────────────────────────────

def _game_headers():
    return {"X-SDTD-API-TOKENNAME": GAME_API_TOKEN, "X-SDTD-API-SECRET": GAME_API_SECRET}


def game_api_get(path, params=None, timeout=10):
    try:
        r = _req.get(f"{GAME_API_URL}{path}", headers=_game_headers(), params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"error": str(e)}


def game_api_post(path, body=None, timeout=10):
    try:
        r = _req.post(f"{GAME_API_URL}{path}", headers=_game_headers(), json=body or {}, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"error": str(e)}


# ─── Config metadata ──────────────────────────────────────────────────────────

_SECTIONS = [
    {"id": "server",      "label": "Server",      "icon": "bi-server",
     "fields": ["ServerName","ServerDescription","ServerWebsiteURL","ServerPassword",
                "ServerLoginConfirmationText","Region","Language",
                "AdminFileName","IgnoreEOSSanctions"]},
    {"id": "network",     "label": "Network",     "icon": "bi-wifi",
     "fields": ["ServerPort","ServerVisibility","ServerMaxPlayerCount","NetworkPingLimit",
                "ServerReservedSlots","ServerReservedSlotsPermission",
                "ServerAdminSlots","ServerAdminSlotsPermission",
                "WebDashboardEnabled","WebDashboardPort","WebDashboardUrl",
                "EACEnabled","BattlEye","HideCommandExecutionLog",
                "MaxUncoveredMapChunksPerPlayer","TerminalWindowEnabled",
                "ServerAllowCrossplay","ServerDisabledNetworkProtocols",
                "ServerMaxWorldTransferSpeedKiBs"]},
    {"id": "telnet",      "label": "Telnet",      "icon": "bi-terminal",
     "fields": ["TelnetEnabled","TelnetPort","TelnetPassword",
                "TelnetFailedLoginLimit","TelnetFailedLoginsBlocktime"]},
    {"id": "platform",    "label": "Platform",    "icon": "bi-hdd-network",
     "fields": ["crossplatform","serverplatforms"]},
    {"id": "gameplay",    "label": "Gameplay",    "icon": "bi-joystick",
     "fields": ["GameWorld","WorldGenSeed","WorldGenSize","GameName","GameMode",
                "PlayerKillingMode","BuildCreate","DayNightLength","DayLightLength",
                "DeathPenalty","DropOnDeath","DropOnQuit",
                "BlockDamagePlayer","BlockDamageAI","BlockDamageAIBM",
                "XPMultiplier","PartySharedKillRange","PlayerSafeZoneLevel","PlayerSafeZoneHours",
                "AllowSpawnNearFriend","PersistentPlayerProfiles","CameraRestrictionMode",
                "BedrollDeadZoneSize","BedrollExpiryTime",
                "UserDataFolder","SaveGameFolder"]},
    {"id": "landclaims",  "label": "Land Claims", "icon": "bi-flag",
     "fields": ["LandClaimSize","LandClaimCount","LandClaimDeadZone",
                "LandClaimExpiryTime","LandClaimDecayMode","LandClaimOfflineDelay",
                "LandClaimOnlineDurabilityModifier","LandClaimOfflineDurabilityModifier"]},
    {"id": "zombies",     "label": "Zombies",     "icon": "bi-virus",
     "fields": ["EnemyDifficulty","EnemySpawnMode","MaxSpawnedZombies",
                "MaxSpawnedAnimals","ServerMaxAllowedViewDistance","MaxQueuedMeshLayers"]},
    {"id": "sandbox",     "label": "Sandbox",     "icon": "bi-sliders2",
     "fields": ["SandboxCode"]},
    {"id": "performance", "label": "Performance", "icon": "bi-cpu",
     "fields": ["CpuUsage","ServerCpuCount","RootDataFolder",
                "DynamicMeshEnabled","DynamicMeshLandClaimOnly",
                "DynamicMeshLandClaimBuffer","DynamicMeshMaxItemCache",
                "EnableMapRendering","MaxChunkAge","SaveDataLimit"]},
    {"id": "mods",        "label": "Mods",        "icon": "bi-puzzle",
     "fields": ["ModsEnabled","ModList"]},
    {"id": "twitch",      "label": "Twitch",      "icon": "bi-twitch",
     "fields": ["TwitchServerPermission","TwitchBloodMoonAllowed"]},
]

_FIELD_TYPES = {
    "WebDashboardEnabled": "boolean",
    "WebDashboardPort": "number",
    "EACEnabled": "boolean", "BattlEye": "boolean", "TerminalWindowEnabled": "boolean",
    "BuildCreate": "boolean", "ModsEnabled": "boolean", "HideCommandExecutionLog": "boolean",
    "ServerAllowCrossplay": "boolean", "IgnoreEOSSanctions": "boolean",
    "TelnetEnabled": "boolean", "TwitchBloodMoonAllowed": "boolean",
    "DynamicMeshEnabled": "boolean", "DynamicMeshLandClaimOnly": "boolean",
    "PersistentPlayerProfiles": "boolean", "EnableMapRendering": "boolean",
    "SandboxCode": "sandbox_code",
    "ServerPort": "number", "ServerMaxPlayerCount": "number", "NetworkPingLimit": "number",
    "ServerReservedSlots": "number", "ServerReservedSlotsPermission": "number",
    "ServerAdminSlots": "number", "ServerAdminSlotsPermission": "number",
    "MaxUncoveredMapChunksPerPlayer": "number",
    "ServerMaxWorldTransferSpeedKiBs": "number",
    "TelnetPort": "number", "TelnetFailedLoginLimit": "number", "TelnetFailedLoginsBlocktime": "number",
    "TwitchServerPermission": "number",
    "DynamicMeshLandClaimBuffer": "number", "DynamicMeshMaxItemCache": "number",
    "LandClaimSize": "number", "LandClaimCount": "number", "LandClaimDeadZone": "number",
    "LandClaimExpiryTime": "number", "LandClaimOfflineDelay": "number",
    "LandClaimOnlineDurabilityModifier": "number", "LandClaimOfflineDurabilityModifier": "number",
    "BedrollDeadZoneSize": "number", "BedrollExpiryTime": "number",
    "MaxChunkAge": "number", "SaveDataLimit": "number",
    "DayNightLength": "number", "DayLightLength": "number", "XPMultiplier": "number",
    "PartySharedKillRange": "number", "PlayerSafeZoneLevel": "number",
    "PlayerSafeZoneHours": "number", "MaxSpawnedZombies": "number",
    "MaxSpawnedAnimals": "number", "ServerMaxAllowedViewDistance": "number",
    "MaxQueuedMeshLayers": "number", "ServerCpuCount": "number",
    "crossplatform": "select", "serverplatforms": "text",
    "ServerVisibility": "select", "Region": "select", "GameMode": "select",
    "GameWorld": "select", "WorldGenSize": "select",
    "PlayerKillingMode": "select", "DeathPenalty": "select", "DropOnDeath": "select",
    "DropOnQuit": "select", "BlockDamagePlayer": "select", "BlockDamageAI": "select",
    "BlockDamageAIBM": "select", "EnemyDifficulty": "select", "EnemySpawnMode": "select",
    "LandClaimDecayMode": "select", "AllowSpawnNearFriend": "select",
    "CameraRestrictionMode": "select",
    "CpuUsage": "select",
}

_PCTS = [{"value": str(v), "label": f"{v}%"} for v in (0,25,50,75,100,150,200)]
_DROP = [{"value":"0","label":"Nothing"},{"value":"1","label":"Everything"},
         {"value":"2","label":"Toolbelt"},{"value":"3","label":"Backpack"},{"value":"4","label":"Delete All"}]

_FIELD_META = {
    "ServerName":                    {"label": "Server Name"},
    "ServerDescription":             {"label": "Description"},
    "ServerWebsiteURL":              {"label": "Website URL"},
    "ServerPassword":                {"label": "Join Password"},
    "ServerLoginConfirmationText":   {"label": "Login Message"},
    "Region":                        {"label": "Region", "options": ["NorthAmericaEast","NorthAmericaWest","CentralAmerica","SouthAmerica","Europe","Russia","Asia","MiddleEast","Africa","Oceania"]},
    "Language":                      {"label": "Language"},
    "ServerPort":                    {"label": "Server Port"},
    "ServerVisibility":              {"label": "Visibility", "options": [{"value":"2","label":"Public"},{"value":"1","label":"Friends Only"},{"value":"0","label":"Not Listed"}]},
    "ServerMaxPlayerCount":          {"label": "Max Players"},
    "NetworkPingLimit":              {"label": "Max Ping (ms)"},
    "ServerReservedSlots":           {"label": "Reserved Slots"},
    "ServerReservedSlotsPermission": {"label": "Reserved Slots Permission"},
    "ServerAdminSlots":              {"label": "Admin Slots"},
    "ServerAdminSlotsPermission":    {"label": "Admin Slots Permission"},
    "WebDashboardEnabled":           {"label": "Web Dashboard / API", "fix_when_false": True,
                                     "fix_notice": "Required for the panel to communicate with the game server (players, commands, stats)."},
    "WebDashboardPort":              {"label": "Web Dashboard Port"},
    "EACEnabled":                    {"label": "Easy Anti-Cheat"},
    "BattlEye":                      {"label": "BattlEye"},
    "HideCommandExecutionLog":       {"label": "Hide Command Log"},
    "MaxUncoveredMapChunksPerPlayer":{"label": "Max Uncovered Map Chunks (per player)"},
    "TerminalWindowEnabled":         {"label": "Terminal Window"},
    "ServerAllowCrossplay":          {"label": "Allow Crossplay"},
    "ServerDisabledNetworkProtocols":{"label": "Disabled Network Protocols"},
    "ServerMaxWorldTransferSpeedKiBs":{"label": "Max World Transfer Speed (KiB/s)"},
    "WebDashboardUrl":               {"label": "Web Dashboard URL"},
    "TelnetEnabled":                 {"label": "Telnet Enabled"},
    "TelnetPort":                    {"label": "Telnet Port"},
    "TelnetPassword":                {"label": "Telnet Password"},
    "TelnetFailedLoginLimit":        {"label": "Failed Login Limit"},
    "TelnetFailedLoginsBlocktime":   {"label": "Failed Login Block Time (s)"},
    "GameWorld":                     {"label": "World"},
    "WorldGenSeed":                  {"label": "World Seed"},
    "WorldGenSize":                  {"label": "World Size", "options": [
                                       {"value": "6144",  "label": "6144 (6k — ~36 km²)"},
                                       {"value": "8192",  "label": "8192 (8k — ~64 km²)"},
                                       {"value": "10240", "label": "10240 (10k — ~100 km²)"},
                                   ]},
    "GameName":                      {"label": "Save Name"},
    "GameMode":                      {"label": "Game Mode", "options": ["GameModeSurvival"]},
    "PlayerKillingMode":             {"label": "Player Killing", "options": [{"value":"0","label":"No Killing"},{"value":"1","label":"Kill Allies Only"},{"value":"2","label":"Kill Strangers Only"},{"value":"3","label":"Kill Everyone"}]},
    "BuildCreate":                   {"label": "Creative Mode"},
    "DayNightLength":                {"label": "Day Length (real minutes)"},
    "DayLightLength":                {"label": "Daylight Hours (in-game)"},
    "DeathPenalty":                  {"label": "Death Penalty", "options": [{"value":"0","label":"None"},{"value":"1","label":"Vanilla"},{"value":"2","label":"Streak"},{"value":"3","label":"Perma-Death"}]},
    "DropOnDeath":                   {"label": "Drop On Death", "options": _DROP},
    "DropOnQuit":                    {"label": "Drop On Quit", "options": _DROP},
    "BlockDamagePlayer":             {"label": "Block Damage (Player)", "options": _PCTS},
    "BlockDamageAI":                 {"label": "Block Damage (AI)", "options": _PCTS},
    "BlockDamageAIBM":               {"label": "Block Damage (Blood Moon)", "options": _PCTS},
    "XPMultiplier":                  {"label": "XP Multiplier"},
    "PartySharedKillRange":          {"label": "Party Kill Share Range"},
    "PlayerSafeZoneLevel":           {"label": "Safe Zone Level"},
    "PlayerSafeZoneHours":           {"label": "Safe Zone Hours"},
    "AllowSpawnNearFriend":          {"label": "Allow Spawn Near Friend", "options": [{"value":"0","label":"Everywhere"},{"value":"1","label":"Near Bedroll Only"},{"value":"2","label":"Near Bedroll or Friend"}]},
    "PersistentPlayerProfiles":      {"label": "Persistent Player Profiles"},
    "CameraRestrictionMode":         {"label": "Camera Restriction Mode", "options": [{"value":"0","label":"None"},{"value":"1","label":"Limited"},{"value":"2","label":"Forced 3rd Person"}]},
    "BedrollDeadZoneSize":           {"label": "Bedroll Dead Zone Size"},
    "BedrollExpiryTime":             {"label": "Bedroll Expiry Time (days)"},
    "UserDataFolder":                {"label": "User Data Folder",
                                     "fix_notice": "If not set to the volume path, save games and worlds are stored inside the container and permanently lost on rebuild or update."},
    "SaveGameFolder":                {"label": "Save Game Folder"},
    "EnemyDifficulty":               {"label": "Enemy Difficulty", "options": [{"value":"0","label":"Normal"},{"value":"1","label":"Feral"}]},
    "EnemySpawnMode":                {"label": "Enemy Spawning", "options": [{"value":"False","label":"Off"},{"value":"True","label":"On"}]},
    "MaxSpawnedZombies":             {"label": "Max Spawned Zombies"},
    "MaxSpawnedAnimals":             {"label": "Max Spawned Animals"},
    "ServerMaxAllowedViewDistance":  {"label": "Max View Distance"},
    "MaxQueuedMeshLayers":           {"label": "Max Mesh Queue Layers"},
    "SandboxCode":                   {"label": "Sandbox Code"},
    "CpuUsage":                      {"label": "CPU Usage", "options": [{"value":"Default","label":"Default"},{"value":"Minimal","label":"Minimal"},{"value":"High","label":"High"},{"value":"Aggressive","label":"Aggressive"}]},
    "ServerCpuCount":                {"label": "Server CPU Count"},
    "RootDataFolder":                {"label": "Root Data Folder"},
    "DynamicMeshEnabled":            {"label": "Dynamic Mesh Enabled"},
    "DynamicMeshLandClaimOnly":      {"label": "Dynamic Mesh for Land Claims Only"},
    "DynamicMeshLandClaimBuffer":    {"label": "Dynamic Mesh Land Claim Buffer"},
    "DynamicMeshMaxItemCache":       {"label": "Dynamic Mesh Max Item Cache"},
    "EnableMapRendering":            {"label": "Enable Map Rendering"},
    "MaxChunkAge":                   {"label": "Max Chunk Age (-1 = unlimited)"},
    "SaveDataLimit":                 {"label": "Save Data Limit (-1 = unlimited)"},
    "LandClaimSize":                 {"label": "Claim Block Size"},
    "LandClaimCount":                {"label": "Claim Blocks Per Player"},
    "LandClaimDeadZone":             {"label": "Min Distance Between Claims"},
    "LandClaimExpiryTime":           {"label": "Claim Expiry Time (days)"},
    "LandClaimDecayMode":            {"label": "Claim Decay Mode", "options": [{"value":"0","label":"Linear"},{"value":"1","label":"Exponential"},{"value":"2","label":"Full Protection"}]},
    "LandClaimOfflineDelay":         {"label": "Offline Protection Delay (mins)"},
    "LandClaimOnlineDurabilityModifier": {"label": "Online Durability Modifier"},
    "LandClaimOfflineDurabilityModifier":{"label": "Offline Durability Modifier"},
    "ModsEnabled":                   {"label": "Mods Enabled"},
    "ModList":                       {"label": "Mod List"},
    "TwitchServerPermission":        {"label": "Twitch Permission Level"},
    "TwitchBloodMoonAllowed":        {"label": "Twitch Blood Moon Votes Allowed"},
    "AdminFileName":                 {"label": "Admin File Name"},
    "IgnoreEOSSanctions":            {"label": "Ignore EOS Sanctions"},
    "crossplatform":                 {"label": "Cross-Platform Backend",
                                     "options": [{"value": "EOS",  "label": "EOS (Epic Online Services)"},
                                                 {"value": "",     "label": "Disabled (Steam only, no internet required)"}]},
    "serverplatforms":               {"label": "Allowed Platforms"},
}

_FIELD_DESCRIPTIONS = {
    "SandboxCode":                   "Generate in-game via New Game → Advanced → Sandbox Options → Copy Code, or use the Sandbox tab to build and reset the code visually.",
    "ServerPassword":                "Leave blank for no password",
    "WorldGenSize":                  "Only applies when GameWorld = RWG",
    "WebDashboardEnabled":           "Required for the panel to communicate with the game server (players, commands, stats). Must be true.",
    "WebDashboardPort":              "Must match the port mapped in docker-compose.yml (default 8080).",
    "EACEnabled":                    "Easy Anti-Cheat — disable for mods that require it",
    "GameName":                      "Also used as the save directory name",
    "DayNightLength":                "Real-time minutes per in-game day",
    "DayLightLength":                "In-game hours of daylight (max 24)",
    "PlayerSafeZoneLevel":           "Player level below which the safe zone applies (0 = disabled)",
    "WorldGenSeed":                  "Any text or number — determines the generated map layout",
    "XPMultiplier":                  "100 = default, 200 = double XP",
    "crossplatform":                 "Set to 'Disabled' to remove the EOS internet requirement on startup. Steam-only — no console cross-play. Stored in platform.cfg, not sdtdserver.xml.",
    "serverplatforms":               "Comma-separated list of allowed platforms (Steam, LAN, XBL, PSN, EOS). XBL/PSN require EOS enabled above. Stored in platform.cfg.",
    "MaxUncoveredMapChunksPerPlayer":"Maximum number of map chunks each player may uncover (-1 = unlimited)",
    "ServerMaxWorldTransferSpeedKiBs":"Maximum speed for world data transfers to clients (KiB/s)",
    "ServerDisabledNetworkProtocols":"Comma-separated list of network protocols to disable (e.g. SteamNetworking)",
    "ServerAllowCrossplay":          "Allow players from non-Steam platforms to join",
    "TelnetPassword":                "Leave blank to disable Telnet authentication",
    "TelnetFailedLoginLimit":        "Number of failed Telnet logins before the IP is blocked",
    "TelnetFailedLoginsBlocktime":   "Duration (seconds) an IP is blocked after too many failed logins",
    "BedrollDeadZoneSize":           "Radius (blocks) around a bedroll where enemies cannot spawn",
    "BedrollExpiryTime":             "Days until an abandoned bedroll expires (0 = never)",
    "LandClaimSize":                 "Side length of each claim block area in world units",
    "LandClaimDeadZone":             "Minimum distance required between two players' claim blocks",
    "LandClaimExpiryTime":           "Days offline before a claim block expires (0 = never)",
    "LandClaimOfflineDelay":         "Minutes after a player goes offline before reduced durability kicks in (0 = immediately)",
    "LandClaimOnlineDurabilityModifier": "Block damage multiplier inside claims when owner is online (higher = tougher)",
    "LandClaimOfflineDurabilityModifier":"Block damage multiplier inside claims when owner is offline (higher = tougher)",
    "DynamicMeshEnabled":            "Enable dynamic mesh loading for improved performance",
    "DynamicMeshLandClaimOnly":      "Only apply dynamic mesh within land claim areas",
    "DynamicMeshLandClaimBuffer":    "Extra chunk radius around claims to keep in the dynamic mesh",
    "DynamicMeshMaxItemCache":       "Maximum number of mesh items held in cache",
    "MaxChunkAge":                   "Frames a chunk stays loaded after players leave (-1 = unlimited)",
    "SaveDataLimit":                 "Maximum save data size in MB (-1 = unlimited)",
    "EnableMapRendering":            "Enable server-side map tile rendering (requires Allocs MapRendering mod)",
    "TwitchServerPermission":        "Minimum permission level required to use Twitch integration commands",
    "TwitchBloodMoonAllowed":        "Allow Twitch viewers to vote for a Blood Moon event",
    "AdminFileName":                 "Filename of the server admin XML file (relative to UserDataFolder/Saves/)",
    "IgnoreEOSSanctions":            "Allow players banned by Epic Online Services to connect",
    "CameraRestrictionMode":         "Restrict allowed camera perspectives for players",
    "WebDashboardUrl":               "Public URL of the web dashboard (used in some in-game links)",
}


# ─── platform.cfg helpers ─────────────────────────────────────────────────────

_PLATFORM_CFG_KEYS = {"crossplatform", "serverplatforms"}

def parse_platform_cfg() -> dict:
    result = {}
    if not PLATFORM_CFG_PATH.exists():
        return result
    with open(PLATFORM_CFG_PATH, "r") as f:
        for line in f:
            line = line.strip()
            if "=" in line:
                k, _, v = line.partition("=")
                result[k.strip()] = v.strip()
    return result

def save_platform_cfg(updates: dict):
    current = parse_platform_cfg()
    current.update(updates)
    lines = []
    # preserve platform= first, then known keys in order
    for key in ("platform", "crossplatform", "serverplatforms"):
        if key in current:
            lines.append(f"{key}={current[key]}\n")
    for key, val in current.items():
        if key not in ("platform", "crossplatform", "serverplatforms"):
            lines.append(f"{key}={val}\n")
    with open(PLATFORM_CFG_PATH, "w") as f:
        f.writelines(lines)


# ─── Config XML helpers ───────────────────────────────────────────────────────

def parse_config():
    if not CONFIG_PATH.exists():
        return {}, []
    tree = ET.parse(CONFIG_PATH)
    root = tree.getroot()
    values, order = {}, []
    for prop in root.findall("property"):
        name  = prop.get("name", "")
        value = prop.get("value", "")
        values[name] = value
        order.append(name)
    return values, order


def save_config(values: dict):
    if not CONFIG_PATH.exists():
        root = ET.Element("ServerSettings")
        for name, value in values.items():
            ET.SubElement(root, "property", name=name, value=str(value))
        ET.ElementTree(root).write(str(CONFIG_PATH), encoding="unicode", xml_declaration=False)
        return
    tree = ET.parse(CONFIG_PATH)
    root = tree.getroot()
    existing = {p.get("name"): p for p in root.findall("property")}
    for name, value in values.items():
        if name in existing:
            existing[name].set("value", str(value))
        else:
            ET.SubElement(root, "property", name=name, value=str(value))
    tree.write(str(CONFIG_PATH), encoding="unicode", xml_declaration=False)


def _get_server_name():
    try:
        values, _ = parse_config()
        return values.get("ServerName", "7D2D Server")
    except Exception:
        return "7D2D Server"


# ─── Admin XML helpers ────────────────────────────────────────────────────────

def parse_admin() -> dict:
    empty = {"users": [], "whitelist": [], "blacklist": [], "commands": [], "apitokens": []}
    if not ADMIN_PATH.exists():
        return empty
    try:
        tree = ET.parse(ADMIN_PATH)
        root = tree.getroot()
    except ET.ParseError:
        return empty

    def attrs(el, *keys):
        return {k: el.get(k, "") for k in keys}

    return {
        "users":     [attrs(u, "platform", "userid", "name", "permission_level") for u in root.findall("users/user")],
        "whitelist": [attrs(u, "platform", "userid", "name")                     for u in root.findall("whitelist/user")],
        "blacklist": [attrs(b, "platform", "userid", "name", "unbandate", "reason") for b in root.findall("blacklist/blacklisted")],
        "commands":  [attrs(p, "cmd", "permission_level")                         for p in root.findall("commands/permission")],
        "apitokens": [attrs(t, "name", "secret", "permission_level")              for t in root.findall("apitokens/token")],
    }


def save_admin(data: dict):
    root = ET.Element("adminTools")

    users_el = ET.SubElement(root, "users")
    for u in data.get("users", []):
        ET.SubElement(users_el, "user", platform=u.get("platform",""), userid=u.get("userid",""),
                      name=u.get("name",""), permission_level=str(u.get("permission_level","1000")))

    wl_el = ET.SubElement(root, "whitelist")
    for u in data.get("whitelist", []):
        ET.SubElement(wl_el, "user", platform=u.get("platform",""), userid=u.get("userid",""))

    bl_el = ET.SubElement(root, "blacklist")
    for b in data.get("blacklist", []):
        ET.SubElement(bl_el, "blacklisted", platform=b.get("platform",""), userid=b.get("userid",""),
                      unbandate=b.get("unbandate",""), reason=b.get("reason",""))

    cmd_el = ET.SubElement(root, "commands")
    for c in data.get("commands", []):
        ET.SubElement(cmd_el, "permission", cmd=c.get("cmd",""),
                      permission_level=str(c.get("permission_level","0")))

    tok_el = ET.SubElement(root, "apitokens")
    for t in data.get("apitokens", []):
        ET.SubElement(tok_el, "token", name=t.get("name",""), secret=t.get("secret",""),
                      permission_level=str(t.get("permission_level","0")))

    ADMIN_PATH.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(str(ADMIN_PATH), encoding="unicode", xml_declaration=True)


# ─── Flask app ────────────────────────────────────────────────────────────────

@app.context_processor
def _inject_globals():
    return {"server_name": _get_server_name(), "server_state": _server_state}


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


# ─── Auth ─────────────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("password") == PANEL_PASSWORD:
            session["logged_in"] = True
            return redirect(url_for("dashboard"))
        return render_template("login.html", error="Invalid password")
    return render_template("login.html", error=None)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    return redirect(url_for("dashboard"))


# ─── Dashboard ────────────────────────────────────────────────────────────────

@app.route("/dashboard")
@login_required
def dashboard():
    return render_template("dashboard.html", installed=_server_installed())


# ─── Allocs Server Fixes ──────────────────────────────────────────────────────

def _load_mods_pack_config() -> dict:
    if MODS_PACK_CONFIG_PATH.exists():
        try:
            return json.loads(MODS_PACK_CONFIG_PATH.read_text())
        except Exception:
            return {}
    return {}


def _save_mods_pack_config(cfg: dict):
    MODS_PACK_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    MODS_PACK_CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


def _installed_mods() -> list:
    mods_dir = SERVERFILES_PATH / "Mods"
    if not mods_dir.is_dir():
        return []
    pack_cfg = _load_mods_pack_config()
    result = []
    for entry in sorted(mods_dir.iterdir()):
        if not entry.is_dir():
            continue
        info_file = entry / "ModInfo.xml"
        if not info_file.exists():
            continue
        try:
            root = ET.parse(info_file).getroot()
            def _val(tag):
                node = root.find(tag)
                return node.get("value", "").strip() if node is not None else ""
            server_side_only = _val("ServerSideOnly").lower() == "true"
            mod = {
                "dir":          entry.name,
                "name":         _val("Name"),
                "display_name": _val("DisplayName") or _val("Name"),
                "version":      _val("Version"),
                "author":       _val("Author"),
                "description":  _val("Description"),
                "website":      _val("Website"),
                "server_side_only": server_side_only,
            }
        except Exception:
            mod = {"dir": entry.name, "name": entry.name,
                   "display_name": entry.name, "version": "",
                   "author": "", "description": "", "website": "",
                   "server_side_only": False}
        default_include = not mod["server_side_only"] and mod["dir"] not in _DEFAULT_EXCLUDED_MODS
        mod["include_in_pack"] = pack_cfg.get(mod["dir"], default_include)
        result.append(mod)
    return result


def _allocs_status():
    mods_dir  = SERVERFILES_PATH / "Mods"
    installed = any(
        (mods_dir / m).exists() or (mods_dir / (m + ".disabled")).exists()
        for m in ALLOCS_MODS
    )
    return {"installed": installed}


@app.route("/api/mods/allocs")
@login_required
def api_allocs_status():
    return jsonify(_allocs_status())


@app.route("/api/mods/allocs/install", methods=["POST"])
@login_required
def api_allocs_install():
    with _server_lock:
        if _server_state != "stopped":
            return jsonify({"error": "Stop the server before installing mods"}), 400
    try:
        r = _req.get(ALLOCS_URL, timeout=60)
        r.raise_for_status()
        mods_dir = SERVERFILES_PATH / "Mods"
        mods_dir.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(r.content)) as tf:
            for mod in ALLOCS_MODS:
                for suffix in ("", ".disabled"):
                    d = mods_dir / (mod + suffix)
                    if d.exists():
                        shutil.rmtree(str(d))
            tf.extractall(SERVERFILES_PATH)
        return jsonify({"ok": True, **_allocs_status()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/mods/allocs/uninstall", methods=["POST"])
@login_required
def api_allocs_uninstall():
    with _server_lock:
        if _server_state != "stopped":
            return jsonify({"error": "Stop the server before removing mods"}), 400
    mods_dir = SERVERFILES_PATH / "Mods"
    for mod in ALLOCS_MODS:
        for suffix in ("", ".disabled"):
            d = mods_dir / (mod + suffix)
            if d.exists():
                shutil.rmtree(str(d))
    return jsonify({"ok": True, **_allocs_status()})


# ─── Client Mod Pack ──────────────────────────────────────────────────────────

def _mods_pack_info():
    if not MODS_PACK_PATH.exists():
        return None
    st = MODS_PACK_PATH.stat()
    return {
        "size":      _fmt_size(st.st_size),
        "generated": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
    }


@app.route("/mods")
@login_required
def mods_page():
    return render_template("mods.html", mods=_installed_mods(), pack_info=_mods_pack_info(),
                           download_url=url_for("mods_download", _external=True))


@app.route("/api/mods/pack/generate", methods=["POST"])
@login_required
def api_mods_pack_generate():
    body = request.get_json(silent=True) or {}
    selection = body.get("selection")
    if selection is not None:
        cfg = _load_mods_pack_config()
        cfg.update({k: bool(v) for k, v in selection.items()})
        _save_mods_pack_config(cfg)

    mods_dir = SERVERFILES_PATH / "Mods"
    included = [m for m in _installed_mods() if m["include_in_pack"]]
    if not included:
        return jsonify({"error": "No mods selected for the client pack"}), 400
    try:
        MODS_PACK_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = MODS_PACK_PATH.with_suffix(".tmp")
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for mod in included:
                mod_path = mods_dir / mod["dir"]
                for file in mod_path.rglob("*"):
                    if file.is_file():
                        zf.write(file, arcname=str(Path(mod["dir"]) / file.relative_to(mod_path)))
        tmp_path.replace(MODS_PACK_PATH)
        return jsonify({"ok": True, "mod_count": len(included), **_mods_pack_info()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/mods/download")
def mods_download():
    if not MODS_PACK_PATH.exists():
        return "No mod pack has been generated yet.", 404
    return send_file(str(MODS_PACK_PATH), as_attachment=True, download_name="mods.zip")


# ─── Inventory ─────────────────────────────────────────────────────────────────

@app.route("/api/inventory/<steam_id>")
@login_required
def api_inventory(steam_id):
    data = game_api_get("/api/getplayerinventory", params={"userid": steam_id})
    if "error" in data:
        return jsonify(data), 502
    return jsonify(data)


@app.route("/api/itemicon/<name>/<tint>")
@login_required
def api_itemicon(name, tint):
    # Serve directly from disk — the game's IconHandler fails on headless servers
    # because the icon atlas (8192×8192) exceeds the null GPU's texture size limit.
    icon_path = SERVERFILES_PATH / "Data" / "ItemIcons" / f"{name}.png"
    if icon_path.exists():
        resp = Response(icon_path.read_bytes(), mimetype="image/png")
        resp.headers["Cache-Control"] = "public, max-age=86400"
        return resp
    # Fall back to game API (works when icons load correctly)
    try:
        r = _req.get(
            f"{GAME_API_URL}/itemicons/{name}__{tint}.png",
            headers=_game_headers(),
            timeout=5,
        )
        if r.status_code == 200:
            resp = Response(r.content, mimetype="image/png")
            resp.headers["Cache-Control"] = "public, max-age=3600"
            return resp
        return Response(status=r.status_code)
    except Exception:
        return Response(status=502)


# ─── Server status ─────────────────────────────────────────────────────────────

@app.route("/api/server/status")
@login_required
def api_server_status():
    with _server_lock:
        state = _server_state

    cfg, _ = parse_config() if CONFIG_PATH.exists() else ({}, [])
    api_enabled  = cfg.get("WebDashboardEnabled", "false").lower() == "true"
    userdata_ok  = cfg.get("UserDataFolder", "") == str(GAMEDATA_PATH)

    stats = {}
    api_ok = False
    if state == "running":
        ss = game_api_get("/api/serverstats")
        if "data" in ss and isinstance(ss["data"], dict):
            api_ok = True
            d = ss["data"]
            stats["day"]      = d.get("gameTime", {}).get("days")
            stats["players"]  = d.get("players")
            stats["hostiles"] = d.get("hostiles")
            stats["animals"]  = d.get("animals")

    return jsonify({"state": state, "installed": _server_installed(),
                    "branch": _installed_branch(),
                    "api_enabled": api_enabled, "api_ok": api_ok,
                    "userdata_ok": userdata_ok,
                    "expected_userdata_path": str(GAMEDATA_PATH),
                    **stats})


@app.route("/api/server/start", methods=["POST"])
@login_required
def api_server_start():
    return jsonify(server_start())


@app.route("/api/server/stop", methods=["POST"])
@login_required
def api_server_stop():
    return jsonify(server_stop())


@app.route("/api/server/restart", methods=["POST"])
@login_required
def api_server_restart():
    def _do():
        server_restart()
    threading.Thread(target=_do, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/server/install", methods=["POST"])
@login_required
def api_server_install():
    body   = request.get_json() or {}
    branch = body.get("branch", GAME_BRANCH)
    return jsonify(server_install(branch))


@app.route("/api/server/verify", methods=["POST"])
@login_required
def api_server_verify():
    return jsonify(server_install(branch=_installed_branch()))


# ─── Console ──────────────────────────────────────────────────────────────────

@app.route("/console")
@login_required
def console():
    return render_template("console.html")


@app.route("/api/log-stream")
@login_required
def api_log_stream():
    def generate():
        q = queue.Queue(maxsize=200)
        snap = list(_log_buffer)
        with _log_subs_lock:
            _log_subs.append(q)
        try:
            for entry in snap[-100:]:
                yield f"event: logLine\ndata: {json.dumps(entry)}\n\n"
            while True:
                try:
                    entry = q.get(timeout=25)
                    yield f"event: logLine\ndata: {json.dumps(entry)}\n\n"
                except queue.Empty:
                    yield "event: ping\ndata: {}\n\n"
        finally:
            with _log_subs_lock:
                try:
                    _log_subs.remove(q)
                except ValueError:
                    pass

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/run-command", methods=["POST"])
@login_required
def api_run_command():
    cmd = (request.get_json() or {}).get("command", "").strip()
    if not cmd:
        return jsonify({"error": "No command"})
    if _server_state != "running":
        return jsonify({"error": "Server not running"})
    result = game_api_post("/api/command", {"command": cmd})
    return jsonify(result)


# ─── Config ───────────────────────────────────────────────────────────────────

def _available_worlds():
    """Scan installed server files + GeneratedWorlds for world directories; fall back to known defaults."""
    worlds_dir = SERVERFILES_PATH / "Data" / "Worlds"
    if worlds_dir.exists():
        found = sorted(d.name for d in worlds_dir.iterdir() if d.is_dir() and d.name != "Empty")
    else:
        found = ["Navezgane", "Pregen06k01", "Pregen06k02", "Pregen08k01", "Pregen08k02"]
    custom = []
    if WORLDS_ROOT.exists():
        custom = sorted(d.name for d in WORLDS_ROOT.iterdir() if d.is_dir())
    return ["RWG"] + found + custom


@app.route("/config")
@login_required
def config():
    values, _ = parse_config() if CONFIG_PATH.exists() else ({}, [])
    values.update(parse_platform_cfg())
    meta = {
        **_FIELD_META,
        "GameWorld":      {**_FIELD_META.get("GameWorld",      {}), "options": _available_worlds()},
        "UserDataFolder": {**_FIELD_META.get("UserDataFolder", {}), "fix_when_not": str(GAMEDATA_PATH)},
    }
    return render_template("config.html", values=values, sections=_SECTIONS,
                           field_meta=meta, field_types=_FIELD_TYPES,
                           descriptions=_FIELD_DESCRIPTIONS,
                           installed_mods=_installed_mods())


@app.route("/api/config", methods=["GET"])
@login_required
def api_config_get():
    values, _ = parse_config() if CONFIG_PATH.exists() else ({}, [])
    values.update(parse_platform_cfg())
    return jsonify(values)


@app.route("/api/config", methods=["POST"])
@login_required
def api_config_save():
    try:
        body = request.get_json() or {}
        new_values = body.get("updates", body)
        xml_updates      = {k: v for k, v in new_values.items() if k not in _PLATFORM_CFG_KEYS}
        platform_updates = {k: v for k, v in new_values.items() if k in _PLATFORM_CFG_KEYS}
        if xml_updates:
            existing, _ = parse_config() if CONFIG_PATH.exists() else ({}, [])
            existing.update(xml_updates)
            save_config(existing)
        if platform_updates and PLATFORM_CFG_PATH.exists():
            save_platform_cfg(platform_updates)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── Saves ────────────────────────────────────────────────────────────────────

def _dir_size(path: Path):
    total = 0
    for f in path.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except Exception:
            pass
    return total


def _fmt_size(b):
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"


def _count_players(save_path: Path):
    players_dir = save_path / "Players"
    if not players_dir.exists():
        return 0
    return sum(1 for f in players_dir.iterdir() if f.suffix == ".ttp")


@app.route("/saves")
@login_required
def saves():
    saves_list = []
    cfg, _ = parse_config() if CONFIG_PATH.exists() else ({}, [])
    active_name = cfg.get("GameName", "")

    if SAVES_ROOT.exists():
        for world_dir in sorted(SAVES_ROOT.iterdir()):
            if not world_dir.is_dir():
                continue
            is_rwg = (WORLDS_ROOT / world_dir.name).is_dir()
            for save_dir in sorted(world_dir.iterdir()):
                if not save_dir.is_dir():
                    continue
                saves_list.append({
                    "world":      world_dir.name,
                    "save":       save_dir.name,
                    "size":       _fmt_size(_dir_size(save_dir)),
                    "players":    _count_players(save_dir),
                    "last_mod":   datetime.fromtimestamp(save_dir.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                    "is_current": save_dir.name == active_name,
                    "is_rwg":     is_rwg,
                })

    return render_template("saves.html", saves=saves_list)


_WIPE_KEEP = {"GeneratedWorld", "map"}

@app.route("/api/saves/wipe", methods=["POST"])
@login_required
def api_saves_wipe():
    path = Path((request.get_json() or {}).get("path", ""))
    if not path.is_dir() or not str(path).startswith(str(SAVES_ROOT)):
        return jsonify({"error": "Invalid path"}), 400
    for item in path.iterdir():
        if item.name not in _WIPE_KEEP:
            shutil.rmtree(item) if item.is_dir() else item.unlink()
    return jsonify({"ok": True})


@app.route("/api/saves/delete", methods=["POST"])
@login_required
def api_saves_delete():
    path = Path((request.get_json() or {}).get("path", ""))
    if not path.is_dir() or not str(path).startswith(str(SAVES_ROOT)):
        return jsonify({"error": "Invalid path"}), 400
    shutil.rmtree(path)
    return jsonify({"ok": True})


@app.route("/api/saves/delete-world", methods=["POST"])
@login_required
def api_saves_delete_world():
    body      = request.get_json() or {}
    save_path = Path(body.get("save_path", ""))
    world_path = Path(body.get("world_path", ""))
    if save_path.is_dir() and str(save_path).startswith(str(SAVES_ROOT)):
        shutil.rmtree(save_path)
    if world_path.is_dir() and str(world_path).startswith(str(WORLDS_ROOT)):
        shutil.rmtree(world_path)
    return jsonify({"ok": True})


# ─── Admin ────────────────────────────────────────────────────────────────────

@app.route("/admin")
@login_required
def admin():
    tele = _load_tele()
    return render_template("admin.html", data=parse_admin(), tele_data=tele)


@app.route("/api/admin", methods=["GET"])
@login_required
def api_admin_get():
    return jsonify(parse_admin())


@app.route("/api/admin/<section>", methods=["POST"])
@login_required
def api_admin_add(section):
    data = parse_admin()
    if section not in data:
        return jsonify({"error": "Unknown section"}), 400
    entry = request.get_json()
    if not entry:
        return jsonify({"error": "No data"}), 400
    data[section].append(entry)
    save_admin(data)
    return jsonify({"ok": True, "data": data[section]})


@app.route("/api/admin/<section>", methods=["DELETE"])
@login_required
def api_admin_delete(section):
    data = parse_admin()
    if section not in data:
        return jsonify({"error": "Unknown section"}), 400
    criteria = request.get_json()
    if not criteria:
        return jsonify({"error": "No criteria"}), 400
    before = len(data[section])
    data[section] = [e for e in data[section]
                     if not all(str(e.get(k)) == str(v) for k, v in criteria.items())]
    if len(data[section]) == before:
        return jsonify({"error": "Entry not found"}), 404
    save_admin(data)
    return jsonify({"ok": True, "data": data[section]})


# ─── Sandbox ──────────────────────────────────────────────────────────────────

@app.route("/sandbox")
@login_required
def sandbox():
    values, _ = parse_config() if CONFIG_PATH.exists() else ({}, [])
    return render_template("sandbox.html", sandbox_code=values.get("SandboxCode", ""))


@app.route("/api/sandbox", methods=["POST"])
@login_required
def api_sandbox_save():
    code = (request.get_json() or {}).get("code", "")
    try:
        existing, _ = parse_config() if CONFIG_PATH.exists() else ({}, [])
        existing["SandboxCode"] = code
        save_config(existing)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── Players ──────────────────────────────────────────────────────────────────

@app.route("/players")
@login_required
def players():
    return render_template("players.html")


@app.route("/api/players")
@login_required
def api_players():
    return jsonify(game_api_get("/api/player"))


# ─── Give items ───────────────────────────────────────────────────────────────

@app.route("/give")
@login_required
def give():
    return render_template("give.html")


@app.route("/api/items")
@login_required
def api_items():
    return jsonify(game_api_get("/api/item", timeout=30))


# ─── Teleport ─────────────────────────────────────────────────────────────────

@app.route("/teleport")
@login_required
def teleport():
    with _teleport_lock:
        tele = _load_tele()
    return render_template("teleport.html", tele_data=tele)


@app.route("/api/teleport-config", methods=["POST"])
@login_required
def api_teleport_config():
    body = request.get_json() or {}
    with _teleport_lock:
        data = _load_tele()
        for k in ("cooldown_seconds", "daily_limit", "max_waypoints_per_player"):
            if k in body:
                data["config"][k] = int(body[k])
        _save_tele(data)
    return jsonify({"ok": True})


@app.route("/api/waypoints")
@login_required
def api_waypoints_get():
    with _teleport_lock:
        data = _load_tele()
    names = dict(data.get("player_names", {}))
    with _player_cache_lock:
        for sid in data.get("waypoints", {}):
            if sid not in names and sid in _player_cache:
                names[sid] = _player_cache[sid].get("name", "")
    return jsonify({"waypoints": data["waypoints"], "player_names": names})


@app.route("/api/waypoints/<steam_id>/<name>", methods=["DELETE"])
@login_required
def api_waypoint_delete(steam_id, name):
    with _teleport_lock:
        data = _load_tele()
        player_wps = data["waypoints"].get(steam_id, {})
        if name not in player_wps:
            return jsonify({"error": "Waypoint not found"}), 404
        del player_wps[name]
        _save_tele(data)
    return jsonify({"ok": True})


@app.route("/api/teleport-player", methods=["POST"])
@login_required
def api_teleport_player():
    body = request.get_json() or {}
    eid, x, y, z = body.get("entityId"), body.get("x"), body.get("y"), body.get("z")
    if None in (eid, x, y, z):
        return jsonify({"error": "Missing fields"}), 400
    result = game_api_post("/api/command", {"command": f"teleportplayer {eid} {x} {y} {z}"})
    return jsonify(result)


# ─── Startup ──────────────────────────────────────────────────────────────────

def _shutdown_handler(signum, frame):
    """Panel is shutting down — game container keeps running independently."""
    _log_push("Panel shutting down…", source="panel")
    os._exit(0)

signal.signal(signal.SIGTERM, _shutdown_handler)
signal.signal(signal.SIGINT,  _shutdown_handler)


def _sync_container_state():
    """On panel startup, sync _server_state with the actual game container state."""
    global _server_state
    try:
        c = _docker_client.containers.get(GAME_CONTAINER_NAME)
        if c.status == "running":
            with _server_lock:
                _server_state = "running"
            threading.Thread(target=_log_tail_reader, daemon=True).start()
    except Exception:
        pass

threading.Thread(target=_sync_container_state, daemon=True).start()
threading.Thread(target=_player_poller, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8090, threaded=True)
