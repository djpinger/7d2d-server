import os
import re
import json
import queue
import shutil
import secrets
import subprocess
import threading
import time
import collections
from datetime import date, datetime
from functools import wraps
from pathlib import Path
from xml.etree import ElementTree as ET

from flask import (Flask, Response, jsonify, redirect, render_template,
                   request, session, stream_with_context, url_for)
import requests as _req

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

PANEL_PASSWORD     = os.environ.get("PANEL_PASSWORD",      "admin")
CONFIG_PATH        = Path(os.environ.get("CONFIG_PATH",    "/config/sdtdserver.xml"))
ADMIN_PATH         = Path(os.environ.get("ADMIN_PATH",     "/gamedata/Saves/serveradmin.xml"))
SAVES_ROOT         = Path(os.environ.get("SAVES_ROOT",     "/gamedata/Saves"))
WORLDS_ROOT        = Path(os.environ.get("WORLDS_ROOT",    "/gamedata/GeneratedWorlds"))
TELEPORT_DATA_PATH = Path(os.environ.get("TELEPORT_DATA_PATH", "/config/teleport_data.json"))
SERVERFILES_PATH   = Path(os.environ.get("SERVERFILES_PATH",   "/serverfiles"))
STEAMCMD           = Path(os.environ.get("STEAMCMD_PATH",       "/opt/steamcmd/steamcmd.sh"))
GAME_BRANCH        = os.environ.get("GAME_BRANCH",    "public")
GAME_API_URL       = os.environ.get("GAME_API_URL",   "http://localhost:8080")
GAME_API_TOKEN     = os.environ.get("GAME_API_TOKEN_NAME", "")
GAME_API_SECRET    = os.environ.get("GAME_API_SECRET", "")


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


# ─── Server process management ────────────────────────────────────────────────

_server_proc  = None
_server_state = "stopped"   # stopped | starting | running | stopping | installing
_server_lock  = threading.Lock()


def _proc_reader(proc):
    """Background thread: reads game server stdout, drives log + chat bot."""
    global _server_state
    try:
        for raw in iter(proc.stdout.readline, ""):
            raw = raw.rstrip()
            if not raw:
                continue
            msg, typ = _parse_raw(raw)
            _log_push(msg, typ)

            # Transition to "running" once the web API is up
            if _server_state == "starting" and "Started Webserver on port" in msg:
                with _server_lock:
                    if _server_state == "starting":
                        _server_state = "running"

            # Chat bot
            cm = _CHAT_RE.search(msg)
            if cm:
                steam_id   = cm.group(1)
                entity_id  = int(cm.group(2))
                player_name = cm.group(4)
                chat_msg   = cm.group(5)
                threading.Thread(
                    target=_handle_chat,
                    args=(steam_id, entity_id, player_name, chat_msg),
                    daemon=True,
                ).start()
    except Exception:
        pass
    finally:
        with _server_lock:
            if _server_state not in ("stopping", "stopped", "installing"):
                _server_state = "stopped"
        _log_push("Server process exited.", "Warning", "panel")


def server_start():
    global _server_proc, _server_state
    with _server_lock:
        if _server_proc and _server_proc.poll() is None:
            return {"error": "Server already running"}
        if _server_state == "installing":
            return {"error": "Installation in progress"}
        exe = SERVERFILES_PATH / "7DaysToDieServer.x86_64"
        if not exe.exists():
            return {"error": "Server not installed — use Install/Update first"}
        _server_state = "starting"

    _log_push("Starting server…", source="panel")
    try:
        proc = subprocess.Popen(
            [str(exe),
             "-logfile", "/dev/stdout",
             "-quit", "-batchmode", "-nographics", "-dedicated",
             f"-configfile={CONFIG_PATH}",
             "-UserDataFolder", str(SERVERFILES_PATH.parent / "gamedata")],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, cwd=str(SERVERFILES_PATH),
        )
        with _server_lock:
            _server_proc = proc
        threading.Thread(target=_proc_reader, args=(proc,), daemon=True).start()
        return {"ok": True}
    except Exception as e:
        with _server_lock:
            _server_state = "stopped"
        return {"error": str(e)}


def server_stop():
    global _server_state
    with _server_lock:
        proc = _server_proc
        if not proc or proc.poll() is not None:
            _server_state = "stopped"
            return {"error": "Server not running"}
        _server_state = "stopping"

    _log_push("Stopping server…", source="panel")
    proc.terminate()
    try:
        proc.wait(timeout=60)
    except subprocess.TimeoutExpired:
        _log_push("Force-killing server (timeout)…", "Warning", "panel")
        proc.kill()
        proc.wait()

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
        proc = _server_proc
        if proc and proc.poll() is None:
            return {"error": "Stop the server before installing"}
        if _server_state == "installing":
            return {"error": "Installation already in progress"}
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


def _installed_branch() -> str:
    """Read the branch from the SteamCMD app manifest. Returns 'public' if not found."""
    manifest = SERVERFILES_PATH / "steamapps" / "appmanifest_294420.acf"
    if not manifest.exists():
        return GAME_BRANCH
    try:
        text = manifest.read_text()
        m = re.search(r'"betakey"\s+"([^"]*)"', text)
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


@app.route("/api/server/status")
@login_required
def api_server_status():
    with _server_lock:
        proc  = _server_proc
        state = _server_state
    pid = proc.pid if proc and proc.poll() is None else None

    stats = {}
    if state == "running":
        gs = game_api_get("/api/gamestats")
        ss = game_api_get("/api/serverstats")
        if "data" in gs:
            d = gs["data"]
            stats["day"]     = d.get("gametime", {}).get("days")
            stats["players"] = d.get("players")
        if "data" in ss:
            d = ss["data"]
            stats["fps"]     = round(d.get("fps", 0), 1)
            stats["heap_mb"] = round(d.get("memUsed", 0) / 1024 / 1024, 0)

    return jsonify({"state": state, "installed": _server_installed(),
                    "branch": _installed_branch(), "pid": pid, **stats})


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

_CONFIG_SECTIONS = {
    "Server":      ["ServerName","ServerDescription","ServerWebsiteURL","ServerPassword",
                    "ServerLoginConfirmationText","Region","Language"],
    "Network":     ["ServerPort","ServerVisibility","NetworkPingLimit","ServerMaxPlayerCount",
                    "ServerReservedSlots","ServerReservedSlotsPermission","ServerAdminSlots",
                    "ServerAdminSlotsPermission","HideCommandExecutionLog","MaxUncoveredMapChunks",
                    "EACEnabled","BattlEye","TerminalWindowEnabled"],
    "Gameplay":    ["GameWorld","WorldGenSeed","WorldGenSize","GameName","GameMode",
                    "PlayerKillingMode","BuildCreate","DayNightLength","DayLightLength",
                    "DeathPenalty","DropOnDeath","DropOnQuit","UserDataFolder",
                    "SaveGameFolder","BlockDamagePlayer","BlockDamageAI","BlockDamageAIBM",
                    "XPMultiplier","PartySharedKillRange","PlayerSafeZoneLevel",
                    "PlayerSafeZoneHours"],
    "Zombies":     ["EnemyDifficulty","EnemySpawnMode","MaxSpawnedZombies","MaxSpawnedAnimals",
                    "ServerMaxAllowedViewDistance","MaxQueuedMeshLayers"],
    "Sandbox":     ["SandboxCode"],
    "Performance": ["CpuUsage","ServerCpuCount","RootDataFolder"],
    "Mods":        ["ModsEnabled","ModList"],
}

@app.route("/config")
@login_required
def config():
    values, _ = parse_config() if CONFIG_PATH.exists() else ({}, [])
    return render_template("config.html", values=values, sections=_CONFIG_SECTIONS)


@app.route("/api/config", methods=["POST"])
@login_required
def api_config_save():
    try:
        new_values = request.get_json() or {}
        existing, _ = parse_config() if CONFIG_PATH.exists() else ({}, [])
        existing.update(new_values)
        save_config(existing)
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
    worlds = []
    active_name, _ = parse_config() if CONFIG_PATH.exists() else ({}, [])
    active_name = (active_name if isinstance(active_name, dict) else {}).get("GameName", "")

    if SAVES_ROOT.exists():
        for world_dir in sorted(SAVES_ROOT.iterdir()):
            if not world_dir.is_dir():
                continue
            world_saves = []
            for save_dir in sorted(world_dir.iterdir()):
                if not save_dir.is_dir():
                    continue
                world_saves.append({
                    "name":        save_dir.name,
                    "path":        str(save_dir),
                    "size":        _fmt_size(_dir_size(save_dir)),
                    "players":     _count_players(save_dir),
                    "modified":    datetime.fromtimestamp(save_dir.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                    "active":      save_dir.name == active_name,
                })
            worlds.append({"name": world_dir.name, "saves": world_saves})

    rwg_worlds = []
    if WORLDS_ROOT.exists():
        for w in sorted(WORLDS_ROOT.iterdir()):
            if w.is_dir():
                rwg_worlds.append({"name": w.name, "path": str(w), "size": _fmt_size(_dir_size(w))})

    return render_template("saves.html", worlds=worlds, rwg_worlds=rwg_worlds)


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
    return render_template("admin.html", tele_data=tele)


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

threading.Thread(target=_player_poller, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8090, threaded=True)
