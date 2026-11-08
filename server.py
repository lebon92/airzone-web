#!/usr/bin/env python3
"""
Airzone Thermostat - small web app to control an Airzone system.

Runs on a computer at home (Windows, Mac, Linux or Raspberry Pi), talks to the
Airzone Local API (http://<ip>:3000/api/v1/hvac) and serves a phone-friendly
control page with presets, boost, schedules, open-window detection and
settings. Uses only the Python standard library: nothing to install.

Usage:
    python server.py
Then open http://localhost:8080

Settings (optional environment variables; the Settings tab overrides the
thermostat address once saved there):
    AIRZONE_IP     IP of the Airzone gateway     (no default: set it here or in Settings)
    AIRZONE_PORT   Port of the Airzone Local API (default 3000)
    PORT           Port this app listens on      (default 8080)
    SCHEDULE_FILE  Where schedules are saved     (default schedules.json next to this file)
    DATA_DIR       Where presets, boosts and settings are saved (default: same folder as SCHEDULE_FILE)
"""
import base64
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LISTEN_PORT = int(os.environ.get("PORT", "8080"))
SCHEDULE_FILE = Path(os.environ.get("SCHEDULE_FILE", Path(__file__).resolve().with_name("schedules.json")))
DATA_DIR = Path(os.environ.get("DATA_DIR", SCHEDULE_FILE.parent))
PRESET_FILE = DATA_DIR / "presets.json"
BOOST_FILE = DATA_DIR / "boosts.json"
WINDOW_FILE = DATA_DIR / "windows.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
ZONE_FILE = DATA_DIR / "zones.json"   # every zone ever seen, so offline zones stay visible

LOOP_SECONDS = float(os.environ.get("LOOP_SECONDS", "20"))      # how often schedules/boosts are checked
SAMPLE_SECONDS = float(os.environ.get("SAMPLE_SECONDS", "60"))  # how often temperatures are recorded
WRITE_GAP = float(os.environ.get("WRITE_GAP", "1.0"))           # pause after each command sent to the thermostat
WINDOW_COOLDOWN = 15 * 60  # after a pause ends, ignore that zone for 15 min while it warms up again
HEAT_MODES = {3}           # Airzone mode 3 = Heating
COOL_MODES = {2}           # Airzone mode 2 = Cooling

ALLOWED_PARAMS = {"on", "setpoint", "mode"}
BOOST_MINUTES = (30, 60, 120, 180)

DEFAULT_PRESETS = [
    {"id": "comfort", "name": "Comfort",
     "rules": [{"zone": "all", "zoneName": "All zones", "action": "on", "setpoint": 21.0}]},
    {"id": "night", "name": "Night",
     "rules": [{"zone": "all", "zoneName": "All zones", "action": "setpoint", "setpoint": 18.0}]},
    {"id": "away", "name": "Away",
     "rules": [{"zone": "all", "zoneName": "All zones", "action": "setpoint", "setpoint": 16.0}]},
    {"id": "alloff", "name": "All off",
     "rules": [{"zone": "all", "zoneName": "All zones", "action": "off", "setpoint": None}]},
]


def default_settings():
    return {
        "airzoneIp": os.environ.get("AIRZONE_IP", "").strip(),
        "airzonePort": int(os.environ.get("AIRZONE_PORT", "3000")),
        "zoneNames": {},
        "zoneOrder": [],
        "hiddenZones": [],
        "window": {"enabled": False, "drop": 1.0, "minutes": 10, "pause": 20, "zones": {}},
    }


_lock = threading.RLock()
_api_lock = threading.Lock()   # one request to the thermostat at a time
_last_write = 0.0              # time.monotonic() of the last command sent
_known = None                  # zone key -> {"data": last zone data, "lastSeen": epoch}, in first-seen order
_known_saved = 0.0
_offline = set()               # keys of zones missing from the last answer (or all, if unreachable)
_activity = []   # recent automatic and manual runs, newest last
_history = {}    # zone key -> [(time, roomTemp), ...] while the zone is heating
_drops = {}      # zone key -> current change the wrong way over the detection window (Settings readout)
_directions = {} # zone key -> "heat" or "cool": what the zone was doing when its readings were taken
_cooldown = {}   # zone key -> time until which detection is ignored


# ----------------------------------------------------------------- Files ---

def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except Exception as e:  # noqa: BLE001
        print(f"Could not read {path}: {e}")
        return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_schedules():
    return load_json(SCHEDULE_FILE, [])


def save_schedules(items):
    save_json(SCHEDULE_FILE, items)


def load_presets():
    if not PRESET_FILE.exists():
        save_json(PRESET_FILE, DEFAULT_PRESETS)
    return load_json(PRESET_FILE, [])


def save_presets(items):
    save_json(PRESET_FILE, items)


def load_settings():
    s = default_settings()
    saved = load_json(SETTINGS_FILE, {})
    for k, v in saved.items():
        if k == "window" and isinstance(v, dict):
            s["window"].update(v)
        else:
            s[k] = v
    return s


def log_activity(text, ok):
    with _lock:
        _activity.append({"at": datetime.now().strftime("%a %d %b %H:%M"), "text": text, "ok": ok})
        del _activity[:-30]
    print(("OK   " if ok else "FAIL ") + text)


# ---------------------------------------------------------------- Airzone ---

NO_ADDRESS = ("No thermostat address set yet. Enter your Airzone gateway's IP address in "
              "Settings → Thermostat connection (or set AIRZONE_IP).")


def base_url(ip=None, port=None):
    s = load_settings()
    ip = ip or s["airzoneIp"]
    if not ip:
        raise RuntimeError(NO_ADDRESS)
    return f"http://{ip}:{port or s['airzonePort']}/api/v1"


def airzone_request(method, path, body, url=None):
    """Send one request. Requests are queued one at a time, and after each
    command (PUT) the next request waits WRITE_GAP seconds: the gateway can
    drop a command that arrives while it is still applying the previous one."""
    global _last_write
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        (url or base_url()) + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    with _api_lock:
        wait = _last_write + WRITE_GAP - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                raw = resp.read().decode() or "{}"
                return json.loads(raw)
        finally:
            if method == "PUT":
                _last_write = time.monotonic()


def put_zone(system_id, zone_id, on=None, setpoint=None, mode=None):
    """Change a zone, one parameter per command. Turning on: on, then mode,
    then temperature. Turning off: temperature and mode first, then off."""
    steps = [("on", on), ("mode", mode), ("setpoint", setpoint)]
    if on == 0:
        steps = [("mode", mode), ("setpoint", setpoint), ("on", on)]
    for name, value in steps:
        if value is not None:
            airzone_request("PUT", "/hvac", {"systemID": system_id, "zoneID": zone_id, name: value})


def fetch_zones(url=None):
    """Return every zone of every system. Tries 'all systems' first, then system 1."""
    url = url or base_url()   # raises a clear message if no address is set
    last_error = None
    for body in ({"systemID": 0, "zoneID": 0}, {"systemID": 1, "zoneID": 0}):
        try:
            res = airzone_request("POST", "/hvac", body, url)
        except Exception as e:  # noqa: BLE001
            last_error = e
            continue
        zones = []
        if isinstance(res.get("systems"), list):
            for system in res["systems"]:
                zones += system.get("data", [])
        elif isinstance(res.get("data"), list):
            zones = res["data"]
        if zones:
            return zones
    raise RuntimeError(f"Could not read zones from Airzone at {url}: {last_error}")


def seen_text(epoch):
    t = datetime.fromtimestamp(epoch)
    return t.strftime("%H:%M") if t.date() == datetime.now().date() else t.strftime("%d %b %H:%M").lstrip("0")


def merge_known(fresh):
    """Remember every zone seen, and return all known zones: the fresh ones,
    plus the missing ones marked offline with their last data."""
    global _known, _known_saved, _offline
    now = time.time()
    with _lock:
        if _known is None:
            _known = {r["key"]: {"data": r["data"], "lastSeen": r["lastSeen"]}
                      for r in load_json(ZONE_FILE, []) if isinstance(r, dict) and "key" in r}
        added = False
        for z in fresh:
            k = zone_key(z)
            added = added or k not in _known
            _known[k] = {"data": dict(z), "lastSeen": now}
        fresh_keys = {zone_key(z): z for z in fresh}
        newly_offline = bool({k for k in _known if k not in fresh_keys} - _offline) if fresh else False
        # Save at once when a zone appears or drops out (so its last readings are kept),
        # otherwise refresh the file every 5 minutes.
        if added or newly_offline or now - _known_saved > 300:
            save_json(ZONE_FILE, [{"key": k, **v} for k, v in _known.items()])
            _known_saved = now
        merged = []
        for k, rec in _known.items():
            if k in fresh_keys:
                z = fresh_keys[k]
                z["offline"] = False
            else:
                z = dict(rec["data"])
                z["offline"] = True
                z["lastSeen"] = seen_text(rec["lastSeen"])
            merged.append(z)
        _offline = {zone_key(z) for z in merged if z["offline"]}
    return merged


def forget_zone(key):
    global _known
    with _lock:
        merge_known([])  # make sure the list is loaded
        if key in _known:
            del _known[key]
            save_json(ZONE_FILE, [{"key": k, **v} for k, v in _known.items()])


def list_zones(strict=True):
    """All known zones (offline ones included and marked), with the display
    names, order and hidden flags chosen in Settings.
    strict=True raises if the thermostat can't be reached (used before acting);
    strict=False returns every zone as offline instead (used by the page).
    Returns the list; with strict=False, also returns the gateway error or None."""
    error = None
    try:
        fresh = fetch_zones()
    except Exception as e:  # noqa: BLE001
        if strict:
            raise
        fresh, error = [], e
    zones = merge_known(fresh)
    if not zones and error:
        raise error
    settings = load_settings()
    names = settings.get("zoneNames", {})
    order = {k: i for i, k in enumerate(settings.get("zoneOrder", []))}
    hidden = set(settings.get("hiddenZones", []))
    for z in zones:
        z["airzoneName"] = z.get("name") or f"Zone {z['zoneID']}"
        z["hidden"] = zone_key(z) in hidden   # display only: the zone keeps working
        alias = names.get(zone_key(z))
        if alias:
            z["name"] = alias
    # Zones not in the saved order (e.g. new ones) go last, in the order they were first seen.
    ordered = [z for _, z in sorted(enumerate(zones), key=lambda iz: (order.get(zone_key(iz[1]), len(order) + iz[0])))]
    return ordered if strict else (ordered, str(error) if error else None)


def zone_key(z):
    return f"{z['systemID']}-{z['zoneID']}"


def split_key(key):
    s, z = key.split("-")
    return int(s), int(z)


def clamp_setpoint(z, value):
    lo, hi = z.get("minTemp", 5), z.get("maxTemp", 35)
    return min(hi, max(lo, float(value)))


def update_zone(payload):
    """Manual change from the Zones screen. A manual on/off or temperature
    change also ends an open-window pause on that zone."""
    system_id = int(payload["systemID"])
    zone_id = int(payload["zoneID"])
    change = {}
    for key, value in payload.items():
        if key in ALLOWED_PARAMS:
            change[key] = float(value) if key == "setpoint" else int(value)
    if not change:
        raise ValueError("Nothing to change")
    put_zone(system_id, zone_id, **change)
    if "on" in change or "setpoint" in change:
        drop_state(WINDOW_FILE, f"{system_id}-{zone_id}")
    return {"ok": True}


def apply_rule(zones, zone, action, setpoint):
    """Apply one action (on / off / setpoint) to one zone or to all zones.
    Any boost or open-window pause on those zones ends: the new action takes over.
    Offline zones are skipped; returns their names."""
    targets = zones if zone == "all" else [z for z in zones if zone_key(z) == zone]
    if not targets:
        raise RuntimeError("zone not found")
    skipped = []
    for z in targets:
        if z.get("offline"):
            skipped.append(z.get("name") or f"Zone {z['zoneID']}")
            continue
        sp = clamp_setpoint(z, setpoint) if setpoint is not None and action != "off" else None
        if action == "off":
            put_zone(z["systemID"], z["zoneID"], on=0)
        elif action == "on":
            put_zone(z["systemID"], z["zoneID"], on=1, setpoint=sp)
        elif sp is not None:
            put_zone(z["systemID"], z["zoneID"], setpoint=sp)
        drop_state(BOOST_FILE, zone_key(z))
        drop_state(WINDOW_FILE, zone_key(z))
    return skipped


def skipped_note(skipped):
    return f" (skipped, offline: {', '.join(skipped)})" if skipped else ""


# ------------------------------------------- Temporary states (boost, window)
# Both are stored as {zone key: {"name", "until", "restore": {"on", "setpoint"}, ...}}
# and end by putting the zone back to "restore".

def load_state(path):
    return load_json(path, {})


def drop_state(path, key):
    """Forget a boost / pause without restoring (another action replaced it)."""
    with _lock:
        items = load_state(path)
        if key in items:
            del items[key]
            save_json(path, items)


def current_restore(key, z):
    """What the zone should return to: the state saved by a running boost or
    pause if there is one (and that boost/pause is forgotten), else its state now."""
    with _lock:
        for path in (BOOST_FILE, WINDOW_FILE):
            item = load_state(path).get(key)
            if item:
                drop_state(path, key)
                return item["restore"]
    return {"on": int(z.get("on", 0)), "setpoint": z.get("setpoint")}


def restore_zone(key, restore):
    system_id, zone_id = split_key(key)
    on, setpoint = int(restore.get("on", 0)), restore.get("setpoint")
    put_zone(system_id, zone_id, on=on, setpoint=setpoint)
    return "off" if not on else f"{(setpoint or 0):.1f}°"


def end_state(path, key, label, reason):
    with _lock:
        item = load_state(path).get(key)
    if not item:
        return
    state = restore_zone(key, item["restore"])
    drop_state(path, key)
    log_activity(f"{label} {item['name']} {reason}: back to {state}", True)


def check_expired(path, label, reason, after=None):
    now = time.time()
    with _lock:
        # A zone that is offline can't be put back yet: wait until it reconnects.
        expired = [k for k, b in load_state(path).items() if b["until"] <= now and k not in _offline]
    for key in expired:
        try:
            end_state(path, key, label, reason)
            if after:
                after(key)
        except Exception as e:  # noqa: BLE001  (retried on next check)
            log_activity(f"Ending {label.lower()} on zone {key} failed: {e}", False)


def state_for_page(path):
    out = {}
    for key, b in load_state(path).items():
        out[key] = {"setpoint": b.get("setpoint"), "drop": b.get("drop"), "direction": b.get("direction", "heat"),
                    "until": datetime.fromtimestamp(b["until"]).strftime("%H:%M"),
                    "minutesLeft": max(0, int((b["until"] - time.time()) // 60))}
    return out


# ------------------------------------------------------------------ Boost ---

def start_boost(key, setpoint, minutes):
    if minutes not in BOOST_MINUTES:
        raise ValueError("Invalid duration")
    z = next((z for z in list_zones() if zone_key(z) == key), None)
    if not z:
        raise ValueError("Zone not found")
    if z.get("offline"):
        raise ValueError(f"{z['name']} is offline")
    target = clamp_setpoint(z, round(float(setpoint) * 2) / 2)
    restore = current_restore(key, z)   # boosting a boosted or paused zone keeps the ORIGINAL state
    put_zone(z["systemID"], z["zoneID"], on=1, setpoint=target)
    with _lock:
        boosts = load_state(BOOST_FILE)
        boosts[key] = {"name": z["name"] or f"Zone {z['zoneID']}", "setpoint": target,
                       "until": time.time() + minutes * 60, "restore": restore}
        save_json(BOOST_FILE, boosts)
    _history.pop(key, None)
    log_activity(f"Boost {boosts[key]['name']}: {target:.1f}° for {minutes} min", True)
    return boosts[key]


# ---------------------------------------------------- Open-window detection ---

def start_window_pause(z, change, direction, cfg):
    """direction "heat": the room cooled down while heating; "cool": it warmed up while cooling."""
    key = zone_key(z)
    restore = current_restore(key, z)
    put_zone(z["systemID"], z["zoneID"], on=0)
    with _lock:
        items = load_state(WINDOW_FILE)
        items[key] = {"name": z["name"] or f"Zone {z['zoneID']}", "until": time.time() + cfg["pause"] * 60,
                      "restore": restore, "drop": round(change, 1), "direction": direction}
        save_json(WINDOW_FILE, items)
    _history.pop(key, None)
    what, paused = ("dropped", "heating") if direction == "heat" else ("rose", "cooling")
    log_activity(f"Open window? {items[key]['name']} {what} {change:.1f}° in {cfg['minutes']} min: "
                 f"{paused} paused {cfg['pause']} min", True)


def after_window(key):
    _cooldown[key] = time.time() + WINDOW_COOLDOWN
    _history.pop(key, None)


def zone_direction(z):
    """"heat" if the zone is on and heating, "cool" if on and cooling, else None.
    Auto, Fan, Dry and Stop are not watched."""
    if z.get("offline") or not z.get("on") or z.get("roomTemp") is None:
        return None
    if z.get("mode") in HEAT_MODES:
        return "heat"
    if z.get("mode") in COOL_MODES:
        return "cool"
    return None


def monitor_tick():
    """Record each heating or cooling zone's temperature, and pause it when the
    room moves fast the wrong way: falling while heating, rising while cooling."""
    cfg = load_settings()["window"]
    zones = list_zones()
    now = time.time()
    paused = load_state(WINDOW_FILE)
    span = cfg["minutes"] * 60
    seen = set()
    for z in zones:
        key = zone_key(z)
        seen.add(key)
        direction = zone_direction(z)
        if not direction or key in paused or _directions.get(key) not in (None, direction):
            # Not watched, paused, or the mode just switched: start again from fresh readings.
            _history.pop(key, None)
            _drops.pop(key, None)
            _directions.pop(key, None)
            if not direction or key in paused:
                continue
        _directions[key] = direction
        hist = _history.setdefault(key, [])
        hist.append((now, float(z["roomTemp"])))
        hist[:] = [(t, v) for t, v in hist if t >= now - span - SAMPLE_SECONDS / 2]
        if len(hist) < 2:
            _drops[key] = None
            continue
        temps = [v for _, v in hist]
        change = (max(temps) - temps[-1]) if direction == "heat" else (temps[-1] - min(temps))
        _drops[key] = round(change, 1)
        watched = cfg["enabled"] and cfg["zones"].get(key, True)
        if watched and change >= cfg["drop"] and now >= _cooldown.get(key, 0):
            try:
                start_window_pause(z, change, direction, cfg)
            except Exception as e:  # noqa: BLE001
                log_activity(f"Pausing {z['name']} for an open window failed: {e}", False)
    for key in list(_history):
        if key not in seen:
            _history.pop(key, None)
            _directions.pop(key, None)


def monitor_for_page():
    out = {}
    for key, hist in _history.items():
        out[key] = {"drop": _drops.get(key), "direction": _directions.get(key),
                    "minutes": round((hist[-1][0] - hist[0][0]) / 60) if hist else 0}
    return out


# ---------------------------------------------------------------- Presets ---

def clean_rule(r):
    zone = str(r.get("zone", "all"))
    if zone != "all" and not re.fullmatch(r"\d+-\d+", zone):
        raise ValueError("Invalid zone")
    action = r.get("action")
    if action not in ("on", "off", "setpoint"):
        raise ValueError("Invalid action")
    setpoint = r.get("setpoint")
    if setpoint in ("", None) or action == "off":
        setpoint = None
    else:
        setpoint = round(float(setpoint) * 2) / 2
        if not 5 <= setpoint <= 35:
            raise ValueError("Temperature must be between 5 and 35")
    if action == "setpoint" and setpoint is None:
        raise ValueError("Enter a temperature for each 'set temperature' line")
    name = str(r.get("zoneName") or ("All zones" if zone == "all" else "Zone " + zone))[:60]
    return {"zone": zone, "zoneName": name, "action": action, "setpoint": setpoint}


def clean_preset(p):
    name = str(p.get("name", "")).strip()[:40]
    if not name:
        raise ValueError("Give the preset a name")
    rules = [clean_rule(r) for r in p.get("rules", [])][:20]
    if not rules:
        raise ValueError("Add at least one line")
    return {"id": str(p.get("id") or uuid.uuid4().hex[:8]), "name": name, "rules": rules}


def apply_preset(preset_id):
    with _lock:
        preset = next((p for p in load_presets() if p["id"] == preset_id), None)
    if not preset:
        raise RuntimeError("preset not found")
    zones = list_zones()
    skipped = []
    # "All zones" lines first, so lines for a single zone can override them.
    for r in sorted(preset["rules"], key=lambda r: r["zone"] != "all"):
        for name in apply_rule(zones, r["zone"], r["action"], r.get("setpoint")):
            if name not in skipped:
                skipped.append(name)
    return preset, skipped


def preset_name(preset_id):
    with _lock:
        p = next((p for p in load_presets() if p["id"] == preset_id), None)
    return p["name"] if p else "(deleted preset)"


# -------------------------------------------------------------- Schedules ---

def clean_schedule(s, presets=None):
    """Validate a schedule sent by the page and return a clean copy."""
    t = str(s.get("time", ""))
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t):
        raise ValueError("Time must be HH:MM")
    days = sorted({int(d) for d in s.get("days", []) if 0 <= int(d) <= 6})
    if not days:
        raise ValueError("Pick at least one day")
    out = {"id": str(s.get("id") or uuid.uuid4().hex[:8]), "enabled": bool(s.get("enabled", True)),
           "time": t, "days": days}
    if s.get("action") == "preset":
        preset_id = str(s.get("presetId", ""))
        known = presets if presets is not None else load_presets()
        if not any(p["id"] == preset_id for p in known):
            raise ValueError("Pick a preset")
        out.update({"zone": "all", "zoneName": "", "action": "preset", "setpoint": None, "presetId": preset_id})
        return out
    out.update(clean_rule(s))
    return out


def describe(s):
    if s["action"] == "preset":
        return f"{s['time']} apply preset {preset_name(s['presetId'])}"
    what = {"on": "turn on", "off": "turn off", "setpoint": "set temperature"}[s["action"]]
    if s.get("setpoint") is not None:
        what += f" at {s['setpoint']:.1f}°" if s["action"] == "on" else f" to {s['setpoint']:.1f}°"
    return f"{s['time']} {s['zoneName']}: {what}"


def run_schedule(s):
    """Run a schedule; returns the names of offline zones it had to skip.
    A schedule for one single zone that is offline counts as failed."""
    if s["action"] == "preset":
        return apply_preset(s["presetId"])[1]
    skipped = apply_rule(list_zones(), s["zone"], s["action"], s.get("setpoint"))
    if skipped and s["zone"] != "all":
        raise RuntimeError(f"{skipped[0]} is offline")
    return skipped


# --------------------------------------------------------------- Settings ---

def clean_settings(p):
    """Validate a partial settings update and return the full new settings."""
    s = load_settings()
    if "airzoneIp" in p:
        ip = str(p["airzoneIp"]).strip()
        if not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", ip):
            raise ValueError("Enter a valid IP address or host name")
        s["airzoneIp"] = ip
    if "airzonePort" in p:
        port = int(p["airzonePort"])
        if not 1 <= port <= 65535:
            raise ValueError("Port must be between 1 and 65535")
        s["airzonePort"] = port
    if "zoneNames" in p:
        names = {}
        for k, v in dict(p["zoneNames"]).items():
            v = str(v).strip()[:40]
            if re.fullmatch(r"\d+-\d+", str(k)) and v:
                names[str(k)] = v
        s["zoneNames"] = names
    if "zoneOrder" in p:
        order = []
        for k in list(p["zoneOrder"]):
            k = str(k)
            if re.fullmatch(r"\d+-\d+", k) and k not in order:
                order.append(k)
        s["zoneOrder"] = order
    if "hiddenZones" in p:
        s["hiddenZones"] = sorted({str(k) for k in list(p["hiddenZones"]) if re.fullmatch(r"\d+-\d+", str(k))})
    if "window" in p:
        w, n = dict(p["window"]), s["window"]
        if "enabled" in w:
            n["enabled"] = bool(w["enabled"])
        if "drop" in w:
            n["drop"] = round(float(w["drop"]) * 10) / 10
            if not 0.3 <= n["drop"] <= 5:
                raise ValueError("Temperature change must be between 0.3 and 5°")
        if "minutes" in w:
            n["minutes"] = int(w["minutes"])
            if not 2 <= n["minutes"] <= 60:
                raise ValueError("Detection time must be between 2 and 60 minutes")
        if "pause" in w:
            n["pause"] = int(w["pause"])
            if not 5 <= n["pause"] <= 120:
                raise ValueError("Pause must be between 5 and 120 minutes")
        if "zones" in w:
            n["zones"] = {str(k): bool(v) for k, v in dict(w["zones"]).items() if re.fullmatch(r"\d+-\d+", str(k))}
    return s


def make_backup():
    with _lock:
        return {"app": "airzone-thermostat", "version": 1, "created": datetime.now().isoformat(timespec="seconds"),
                "settings": load_json(SETTINGS_FILE, {}), "presets": load_presets(), "schedules": load_schedules()}


def restore_backup(b):
    if not isinstance(b, dict) or b.get("app") != "airzone-thermostat":
        raise ValueError("This file is not a backup from this app")
    presets = [clean_preset(p) for p in b.get("presets", [])]
    schedules = [clean_schedule(s, presets) for s in b.get("schedules", [])]
    with _lock:
        settings_saved = b.get("settings") or {}
        save_json(PRESET_FILE, presets)
        save_json(SCHEDULE_FILE, sorted(schedules, key=lambda x: x["time"]))
        save_json(SETTINGS_FILE, {})
        save_json(SETTINGS_FILE, clean_settings(settings_saved))
    log_activity(f"Backup restored: {len(presets)} presets, {len(schedules)} schedules", True)
    return {"presets": len(presets), "schedules": len(schedules)}


# ------------------------------------------------------------ Background ---

def scheduler_loop():
    """End expired boosts and pauses, record temperatures, and run each
    enabled schedule once in its minute."""
    done = {}  # schedule id -> "YYYY-MM-DD HH:MM" it last ran successfully
    last_sample = 0
    while True:
        check_expired(BOOST_FILE, "Boost", "ended")
        check_expired(WINDOW_FILE, "Window pause", "ended", after_window)
        if time.time() - last_sample >= SAMPLE_SECONDS:
            last_sample = time.time()
            try:
                monitor_tick()
            except Exception as e:  # noqa: BLE001  (thermostat unreachable: try again next time)
                print(f"Temperature check failed: {e}")
        now = datetime.now()
        hm, day, stamp = now.strftime("%H:%M"), now.weekday(), now.strftime("%Y-%m-%d %H:%M")
        with _lock:
            items = load_schedules()
        for s in items:
            if not s.get("enabled", True) or s["time"] != hm or day not in s["days"]:
                continue
            if done.get(s["id"]) == stamp:
                continue
            try:
                skipped = run_schedule(s)
                done[s["id"]] = stamp
                log_activity(describe(s) + skipped_note(skipped), True)
            except Exception as e:  # noqa: BLE001  (retried on next check this minute)
                log_activity(f"{describe(s)} - failed: {e}", False)
        time.sleep(LOOP_SECONDS)


# ------------------------------------------------------------------- HTTP ---

class Handler(BaseHTTPRequestHandler):
    def _send(self, code, content, ctype="application/json", extra=None):
        data = content.encode() if isinstance(content, str) else content
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj))

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, PAGE, "text/html")
        elif self.path.startswith("/fonts/") and self.path[7:].removesuffix(".woff2") in FONTS:
            data = base64.b64decode(FONTS[self.path[7:].removesuffix(".woff2")])
            self.send_response(200)
            self.send_header("Content-Type", "font/woff2")
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif self.path == "/api/zones":
            try:
                zones, gateway_error = list_zones(strict=False)
                self._json(200, {"zones": zones, "gatewayError": gateway_error,
                                 "boosts": state_for_page(BOOST_FILE), "windows": state_for_page(WINDOW_FILE)})
            except Exception as e:  # noqa: BLE001  (unreachable and no zone remembered yet)
                self._json(502, {"error": str(e)})
        elif self.path == "/api/presets":
            with _lock:
                self._json(200, {"presets": load_presets()})
        elif self.path == "/api/schedules":
            with _lock:
                items = load_schedules()
                activity = list(reversed(_activity))
            self._json(200, {"schedules": items, "activity": activity,
                             "now": datetime.now().strftime("%a %H:%M")})
        elif self.path == "/api/settings":
            try:
                found, error = list_zones(strict=False)
                zones = [{"key": zone_key(z), "airzoneName": z["airzoneName"], "name": z["name"], "hidden": z["hidden"],
                          "offline": z["offline"], "lastSeen": z.get("lastSeen")} for z in found]
            except Exception as e:  # noqa: BLE001
                zones, error = [], str(e)
            self._json(200, {"settings": load_settings(), "zones": zones, "zonesError": error,
                             "monitor": monitor_for_page(), "windows": state_for_page(WINDOW_FILE),
                             "sampleSeconds": SAMPLE_SECONDS})
        elif self.path == "/api/backup":
            name = f"airzone-backup-{datetime.now():%Y-%m-%d}.json"
            self._send(200, json.dumps(make_backup(), indent=2), "application/json",
                       {"Content-Disposition": f'attachment; filename="{name}"'})
        else:
            self._json(404, {"error": "Not found"})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
            p = self.path

            if p == "/api/zone":
                return self._json(200, {"result": update_zone(payload)})

            # ---- presets
            if p == "/api/presets":                    # create or update
                preset = clean_preset(payload)
                with _lock:
                    items = load_presets()
                    idx = next((i for i, x in enumerate(items) if x["id"] == preset["id"]), None)
                    if idx is None:
                        items.append(preset)
                    else:
                        items[idx] = preset
                    save_presets(items)
                return self._json(200, {"preset": preset})
            if p == "/api/presets/delete":
                pid = str(payload.get("id"))
                with _lock:
                    used = sum(1 for s in load_schedules() if s.get("presetId") == pid)
                    if used:
                        raise ValueError(f"This preset is used by {used} schedule(s). Change or delete them first.")
                    save_presets([x for x in load_presets() if x["id"] != pid])
                return self._json(200, {"ok": True})
            if p == "/api/presets/apply":
                preset, skipped = apply_preset(str(payload.get("id")))
                log_activity(f"Preset {preset['name']} applied" + skipped_note(skipped), True)
                return self._json(200, {"ok": True, "skipped": skipped})

            # ---- boost and window pause
            if p == "/api/boost":
                b = start_boost(str(payload.get("key")), payload.get("setpoint"), int(payload.get("minutes", 0)))
                return self._json(200, {"boost": b})
            if p == "/api/boost/cancel":
                end_state(BOOST_FILE, str(payload.get("key")), "Boost", "cancelled")
                return self._json(200, {"ok": True})
            if p == "/api/zones/forget":               # remove a zone that is gone for good
                key = str(payload.get("key"))
                if key not in _offline:
                    raise ValueError("Only an offline zone can be forgotten")
                forget_zone(key)
                return self._json(200, {"ok": True})
            if p == "/api/window/resume":
                key = str(payload.get("key"))
                end_state(WINDOW_FILE, key, "Window pause", "resumed by hand")
                after_window(key)
                return self._json(200, {"ok": True})

            # ---- schedules
            if p == "/api/schedules":                  # create or update
                s = clean_schedule(payload)
                with _lock:
                    items = [x for x in load_schedules() if x["id"] != s["id"]]
                    items.append(s)
                    items.sort(key=lambda x: x["time"])
                    save_schedules(items)
                return self._json(200, {"schedule": s})
            if p == "/api/schedules/delete":
                with _lock:
                    save_schedules([x for x in load_schedules() if x["id"] != str(payload.get("id"))])
                return self._json(200, {"ok": True})
            if p == "/api/schedules/run":              # "Run now" button
                with _lock:
                    s = next((x for x in load_schedules() if x["id"] == str(payload.get("id"))), None)
                if not s:
                    raise ValueError("Schedule not found")
                skipped = run_schedule(s)
                log_activity(describe(s) + " (run manually)" + skipped_note(skipped), True)
                return self._json(200, {"ok": True, "skipped": skipped})

            # ---- settings and backup
            if p == "/api/settings":
                with _lock:
                    s = clean_settings(payload)
                    save_json(SETTINGS_FILE, s)
                return self._json(200, {"settings": s})
            if p == "/api/settings/test":
                s = clean_settings({k: payload[k] for k in ("airzoneIp", "airzonePort") if k in payload})
                zones = fetch_zones(base_url(s["airzoneIp"], s["airzonePort"]))
                return self._json(200, {"zones": len(zones)})
            if p == "/api/backup/restore":
                return self._json(200, restore_backup(payload))

            self._json(404, {"error": "Not found"})
        except (ValueError, KeyError, TypeError) as e:
            self._json(400, {"error": str(e)})
        except urllib.error.HTTPError as e:
            self._json(502, {"error": f"Airzone replied {e.code}: {e.read().decode(errors='ignore')}"})
        except Exception as e:  # noqa: BLE001
            self._json(502, {"error": str(e)})

    def log_message(self, fmt, *args):
        print(f"[{self.address_string()}] {fmt % args}")


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0D1322">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>Thermostat</title>
<script>
(function () {
  var mq = window.matchMedia("(prefers-color-scheme: light)");
  window.applyMode = function (mode) {
    var light = mode === "light" || (mode === "auto" && mq.matches);
    document.documentElement.dataset.mode = mode;
    document.documentElement.dataset.scheme = light ? "light" : "dark";
    var tc = document.querySelector('meta[name="theme-color"]');
    if (tc) tc.content = light ? "#D9E1EC" : "#0D1322";
  };
  var saved = "auto";
  try { saved = localStorage.getItem("mode") || "auto"; } catch (e) {}
  applyMode(saved);
  mq.addEventListener("change", function () { applyMode(document.documentElement.dataset.mode); });
})();
</script>
<style>
  @font-face { font-family:"Big Shoulders Display"; font-weight:300; font-display:swap; src:url(/fonts/bsd-300.woff2) format("woff2"); }
  @font-face { font-family:"Big Shoulders Display"; font-weight:400; font-display:swap; src:url(/fonts/bsd-400.woff2) format("woff2"); }
  @font-face { font-family:"Big Shoulders Display"; font-weight:600; font-display:swap; src:url(/fonts/bsd-600.woff2) format("woff2"); }
  @font-face { font-family:"Onest"; font-weight:400; font-display:swap; src:url(/fonts/onest-400.woff2) format("woff2"); }
  @font-face { font-family:"Onest"; font-weight:500; font-display:swap; src:url(/fonts/onest-500.woff2) format("woff2"); }
  @font-face { font-family:"Onest"; font-weight:600; font-display:swap; src:url(/fonts/onest-600.woff2) format("woff2"); }

  :root {
    color-scheme: dark;
    --bg:#0D1322; --surface:rgba(255,255,255,.075); --raised:rgba(255,255,255,.11); --line:rgba(255,255,255,.15);
    --text:#EEF2F8; --muted:#9FAABB; --field:rgba(0,0,0,.20);
    --heat:#FF8A3D; --cool:#5CC8FF; --boost:#FFC247; --window:#9FB7FF; --ok:#3FCF8E; --err:#FF7070; --amber-btn:#FFC247;
    --glass-hi:rgba(255,255,255,.30); --glass-shadow:0 12px 40px rgba(0,0,0,.35);
    --sheen-a:rgba(255,255,255,.16); --sheen-b:rgba(255,255,255,.05);
    --blob1:rgba(255,138,61,.55); --blob2:rgba(92,200,255,.45); --blob3:rgba(150,120,255,.40);
    --switch-on:rgba(52,199,89,.88);
    --num:"Big Shoulders Display","Barlow Condensed","Arial Narrow",sans-serif;
    --ui:"Onest",system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  }
  :root[data-scheme="light"] {
    color-scheme: light;
    --bg:#D9E1EC; --surface:rgba(255,255,255,.52); --raised:rgba(255,255,255,.62); --line:rgba(255,255,255,.75);
    --text:#141C28; --muted:#566376; --field:rgba(255,255,255,.62);
    --heat:#DD5C17; --cool:#1786C4; --boost:#A87300; --window:#4A6BDA; --ok:#1F9D63; --err:#D23B3B;
    --glass-hi:rgba(255,255,255,.95); --glass-shadow:0 10px 34px rgba(40,55,90,.14);
    --sheen-a:rgba(255,255,255,.55); --sheen-b:rgba(255,255,255,.15);
    --blob1:rgba(255,160,100,.55); --blob2:rgba(110,190,255,.55); --blob3:rgba(175,150,255,.45);
    --switch-on:rgba(48,190,85,.92);
  }
  html { background:var(--bg); }

  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text); font-family:var(--ui); font-size:15px; line-height:1.4;
         -webkit-font-smoothing:antialiased; -webkit-tap-highlight-color:transparent; }
  button, input, select { font-family:inherit; }
  :focus-visible { outline:2px solid var(--cool); outline-offset:2px; }
  [hidden] { display:none !important; }

  header { display:flex; justify-content:space-between; align-items:center; gap:12px;
           padding:22px 16px 10px; max-width:640px; margin:auto; }
  h1 { font-family:var(--num); font-weight:600; font-size:32px; letter-spacing:.01em; margin:0; line-height:1; }
  h2 { font-size:15px; color:var(--muted); font-weight:500; margin:14px 2px 0; }
  #status { font-size:12px; color:var(--muted); padding:6px 11px; border-radius:999px;
            background:var(--surface); border:1px solid var(--line); white-space:nowrap; }
  #status.err { color:var(--err); }

  nav { max-width:608px; margin:4px auto 0; display:flex; gap:4px; padding:4px; border-radius:16px;
        background:var(--surface); border:1px solid var(--line); width:calc(100% - 32px); }
  nav button { flex:1; padding:9px 4px; border:none; border-radius:12px; background:transparent;
               color:var(--muted); font-size:14px; font-weight:600; cursor:pointer; transition:background .15s, color .15s; }
  nav button.active { background:var(--text); color:var(--bg); }

  main { max-width:640px; margin:auto; padding:14px 16px 48px; display:grid; gap:14px; }
  .list { display:grid; gap:12px; }

  /* cards */
  .card { position:relative; overflow:hidden; background:var(--surface); border:1px solid var(--line);
          border-radius:22px; padding:18px 18px 16px; --glow:transparent; }
  .card > * { position:relative; }
  .card::before { content:""; position:absolute; left:50%; top:30px; width:340px; height:210px; transform:translateX(-50%);
                  background:radial-gradient(closest-side, color-mix(in srgb, var(--glow) 26%, transparent), transparent);
                  pointer-events:none; transition:opacity .3s; }
  .card.heat { --glow:var(--heat); } .card.cool { --glow:var(--cool); }
  .card.boosted { --glow:var(--boost); } .card.paused { --glow:var(--window); }
  .top { display:flex; justify-content:space-between; align-items:flex-start; gap:12px; }
  .name { font-size:17px; font-weight:600; letter-spacing:-.005em; }
  .state { display:inline-flex; align-items:center; gap:6px; font-size:13px; font-weight:500; color:var(--muted); margin-top:3px; }
  .state::before { content:""; width:7px; height:7px; border-radius:50%; background:currentColor; }
  .card.heat .state { color:var(--heat); } .card.cool .state { color:var(--cool); }
  .card.offline .state { color:var(--err); }
  .card.offline .set, .card.offline .target, .card.offline .switch, .card.offline select.mode { opacity:.38; }
  .card.offline .top .now { font-style:italic; }
  button:disabled, input:disabled + .slider, select:disabled { cursor:not-allowed; }
  .card.boosted .state { color:var(--boost); } .card.paused .state { color:var(--window); }
  .now { color:var(--muted); font-size:14px; margin-top:2px; }

  .set { display:flex; align-items:center; justify-content:center; gap:26px; margin:14px 0 10px; }
  .set button { width:52px; height:52px; border-radius:50%; border:1px solid var(--line); font-size:24px; line-height:1;
                background:var(--raised); color:var(--text); cursor:pointer; transition:transform .08s; }
  .set button:active { transform:scale(.94); }
  .target { font-family:var(--num); font-weight:300; font-size:80px; line-height:.95; min-width:150px; text-align:center;
            font-variant-numeric:tabular-nums; letter-spacing:-.01em; }
  .target small { font-family:var(--ui); font-size:16px; font-weight:500; color:var(--muted); margin-left:2px; vertical-align:top;
                  position:relative; top:12px; }
  .card.off .set, .card.off .target, .card.paused .set, .card.paused .target { opacity:.38; }

  select, input[type=time], input[type=number], input[type=text] {
    width:100%; margin-top:6px; padding:11px 12px; border-radius:12px; border:1px solid transparent;
    background:var(--raised); color:var(--text); font-size:16px; }
  select:focus, input:focus { border-color:var(--muted); outline:none; }
  input:disabled { opacity:.4; }

  .switch { position:relative; width:50px; height:30px; flex:none; }
  .switch input { opacity:0; width:0; height:0; position:absolute; }
  .slider { position:absolute; inset:0; background:var(--raised); border:1px solid var(--line); border-radius:30px;
            cursor:pointer; transition:background .2s; }
  .slider:before { content:""; position:absolute; width:22px; height:22px; left:3px; top:3px;
                   background:var(--muted); border-radius:50%; transition:transform .2s, background .2s; }
  input:checked + .slider { background:var(--text); border-color:var(--text); }
  input:checked + .slider:before { transform:translateX(20px); background:var(--bg); }
  .switch input:focus-visible + .slider { outline:2px solid var(--cool); outline-offset:2px; }

  .empty { color:var(--muted); text-align:center; padding:36px 0; }
  .btn { padding:10px 15px; border-radius:12px; border:none; background:var(--raised); color:var(--text);
         font-size:14px; font-weight:500; cursor:pointer; }
  .btn.primary { background:var(--text); color:var(--bg); font-weight:600; }
  .btn.danger { color:var(--err); }
  .btn.wide { width:100%; padding:14px; font-size:15px; border-radius:16px; }
  .btn.boostbtn { color:var(--boost); background:color-mix(in srgb, var(--boost) 13%, transparent); }
  .actions { display:flex; gap:8px; margin-top:12px; flex-wrap:wrap; }

  .chips { display:flex; gap:8px; overflow-x:auto; padding:2px 0; scrollbar-width:none; }
  .chips::-webkit-scrollbar { display:none; }
  .chips button { flex:none; padding:9px 16px; border-radius:999px; border:1px solid var(--line); background:var(--surface);
                  color:var(--text); font-size:14px; font-weight:500; cursor:pointer; white-space:nowrap; }
  .chips button:active { background:var(--raised); }

  .banner { display:flex; justify-content:space-between; align-items:center; gap:12px; margin-top:12px;
            padding:10px 10px 10px 14px; border-radius:14px; font-size:14px; font-weight:500; }
  .banner .btn { background:color-mix(in srgb, currentColor 16%, transparent); color:inherit; flex:none; }
  .banner.boost { background:color-mix(in srgb, var(--boost) 12%, transparent); color:var(--boost); }
  .banner.window { background:color-mix(in srgb, var(--window) 12%, transparent); color:var(--window); }

  .boostpanel { margin-top:12px; padding:16px 14px 12px; border-radius:16px;
                background:color-mix(in srgb, var(--boost) 7%, var(--surface));
                border:1px solid color-mix(in srgb, var(--boost) 28%, transparent); }
  .boostpanel .row { display:flex; align-items:center; justify-content:center; gap:20px; }
  .boostpanel .row button.step { width:42px; height:42px; border-radius:50%; border:1px solid var(--line); font-size:20px;
                                 background:var(--raised); color:var(--text); cursor:pointer; }
  .boostpanel .btemp { font-family:var(--num); font-size:48px; font-weight:400; line-height:1; min-width:110px; text-align:center;
                       font-variant-numeric:tabular-nums; color:var(--boost); }
  .durations { display:grid; grid-template-columns:repeat(4,1fr); gap:8px; margin-top:14px; }
  .durations button { padding:11px 0; border:none; border-radius:12px; background:var(--amber-btn); color:#1E1500;
                      font-weight:600; font-size:14px; cursor:pointer; }
  .hint { color:var(--muted); font-size:13px; margin-top:8px; }
  .center { text-align:center; }

  /* schedules & presets */
  .sched.disabled { opacity:.45; }
  .stime { font-family:var(--num); font-size:44px; font-weight:400; line-height:1; font-variant-numeric:tabular-nums; }
  .sdesc { font-size:15px; margin-top:4px; }
  .field { margin-top:14px; }
  .field > label { font-size:13px; color:var(--muted); }
  .days { display:flex; gap:6px; margin-top:6px; }
  .days button { flex:1; min-width:0; padding:10px 0; border:none; border-radius:10px; background:var(--raised);
                 color:var(--muted); font-size:13px; font-weight:500; cursor:pointer; }
  .days button.on { background:var(--text); color:var(--bg); }
  .presets { margin-top:9px; display:flex; gap:14px; }
  .presets a { color:var(--text); font-size:13px; cursor:pointer; text-decoration:underline;
               text-decoration-color:var(--muted); text-underline-offset:3px; }
  .formErr { color:var(--err); font-size:14px; margin-top:10px; min-height:1em; }
  .formOk { color:var(--ok); font-size:14px; margin-top:10px; min-height:1em; }
  .rule { display:grid; grid-template-columns:1fr 84px 42px; gap:6px; align-items:end; margin-top:10px;
          padding:10px; border-radius:14px; background:color-mix(in srgb, var(--raised) 45%, var(--surface)); }
  .rule select, .rule input { margin-top:0; }
  .rule select[data-f="zone"] { grid-column:1 / -1; }
  .rule .x { height:44px; border:none; border-radius:12px; background:var(--raised); color:var(--err); font-size:18px; cursor:pointer; }
  ul.rules { margin:8px 0 0; padding-left:18px; color:var(--muted); font-size:14px; }
  ul.log { list-style:none; margin:0; padding:0; }
  ul.log li { padding:9px 0; border-bottom:1px solid var(--line); font-size:14px; }
  ul.log li:last-child { border-bottom:none; }
  ul.log li span { color:var(--muted); font-size:12px; display:block; }
  ul.log li.fail { color:var(--err); }
  .note { color:var(--muted); font-size:12px; text-align:center; }

  /* settings */
  .grid2 { display:grid; grid-template-columns:1fr 110px; gap:8px; }
  .grid3 { display:grid; grid-template-columns:1fr 1fr 1fr; gap:8px; }
  .zrow { display:flex; align-items:center; justify-content:space-between; gap:12px; padding:11px 0;
          border-bottom:1px solid var(--line); }
  .zrow:last-child { border-bottom:none; }
  .zrow .live { color:var(--muted); font-size:13px; margin-top:2px; }
  .zrow .live.hot { color:var(--window); }
  .togglerow { display:flex; align-items:center; justify-content:space-between; gap:12px; margin-top:12px; }

  @media (prefers-reduced-motion: reduce) { * { transition:none !important; } }

  /* ================= Liquid glass ================= */
  html body { background:transparent; }
  html body::before {
    content:""; position:fixed; inset:-15%; z-index:-1; pointer-events:none;
    background:
      radial-gradient(38% 32% at 12% 18%, var(--blob1), transparent 70%),
      radial-gradient(42% 36% at 88% 34%, var(--blob2), transparent 70%),
      radial-gradient(48% 40% at 40% 92%, var(--blob3), transparent 70%),
      var(--bg);
    filter:blur(30px);
  }
  html .card, html nav, html #status,
  html .chips button {
    background:var(--surface); border:1px solid var(--line);
    -webkit-backdrop-filter:blur(24px) saturate(175%); backdrop-filter:blur(24px) saturate(175%);
    box-shadow:inset 0 1px 0 var(--glass-hi), var(--glass-shadow);
  }
  html .chips { padding:2px 2px 10px; margin:0 -2px -8px; }
  html .chips button { -webkit-backdrop-filter:none; backdrop-filter:none;
    box-shadow:inset 0 1px 0 var(--glass-hi), 0 4px 14px rgba(0,0,0,.14); }
  html .card { border-radius:30px; }
  html .card > * { z-index:1; }
  html .card::after {
    content:""; position:absolute; inset:0; border-radius:inherit; pointer-events:none; z-index:0;
    background:linear-gradient(140deg, var(--sheen-a), transparent 38%, transparent 72%, var(--sheen-b));
  }
  html .card::before {
    z-index:0; width:380px; height:240px;
    background:radial-gradient(closest-side, color-mix(in srgb, var(--glow) 42%, transparent), transparent);
  }
  html nav { border-radius:22px; padding:5px; }
  html nav button { border-radius:17px; }
  html nav button.active {
    background:rgba(255,255,255,.20); color:var(--text);
    box-shadow:inset 0 1px 0 var(--glass-hi), 0 2px 10px rgba(0,0,0,.18);
  }
  :root[data-scheme="light"] nav button.active { background:rgba(255,255,255,.88); }
  html .btn, html .set button,
  html .boostpanel .row button.step, html .rule .x, html .move button {
    background:var(--raised); border:1px solid var(--line);
    box-shadow:inset 0 1px 0 var(--glass-hi);
    -webkit-backdrop-filter:blur(12px); backdrop-filter:blur(12px);
  }
  html .btn { border-radius:14px; }
  html .btn.primary { background:var(--text); color:var(--bg); border-color:transparent; }
  html .btn.danger { color:var(--err); }
  html .btn.wide { border-radius:20px; }
  html .btn.boostbtn {
    color:var(--boost); background:color-mix(in srgb, var(--boost) 16%, transparent);
    border-color:color-mix(in srgb, var(--boost) 32%, transparent);
  }
  html select, html input[type=time],
  html input[type=number], html input[type=text] {
    background:var(--field); border:1px solid var(--line); border-radius:14px;
  }
  html .slider { background:var(--field); border:1px solid var(--line); }
  html .slider:before { background:#fff; box-shadow:0 2px 6px rgba(0,0,0,.30); }
  html input:checked + .slider { background:var(--switch-on); border-color:transparent; }
  html input:checked + .slider:before { background:#fff; }
  html .banner {
    border:1px solid color-mix(in srgb, currentColor 30%, transparent);
    -webkit-backdrop-filter:blur(14px); backdrop-filter:blur(14px);
  }
  html .banner.boost { background:color-mix(in srgb, var(--boost) 16%, transparent); }
  html .banner.window { background:color-mix(in srgb, var(--window) 16%, transparent); }
  html .boostpanel {
    background:color-mix(in srgb, var(--boost) 10%, transparent); border-radius:22px;
    border:1px solid color-mix(in srgb, var(--boost) 34%, transparent); box-shadow:inset 0 1px 0 var(--glass-hi);
  }
  html .rule { background:rgba(255,255,255,.05); border:1px solid var(--line); }
  html .days button { background:var(--field); }
  html .days button.on { background:var(--text); color:var(--bg); }
  html ul.log li, html .zrow { border-color:var(--line); }
  @supports not ((backdrop-filter:blur(1px)) or (-webkit-backdrop-filter:blur(1px))) {
    :root { --surface:rgba(24,32,48,.94); --raised:rgba(40,50,68,.95); }
    :root[data-scheme="light"] { --surface:rgba(255,255,255,.95); --raised:rgba(236,240,245,.97); }
  }

  /* appearance switch */
  .seg { display:flex; gap:6px; margin-top:12px; }
  .seg button { flex:1; padding:11px 0; border-radius:12px; border:1px solid var(--line); background:var(--raised);
                color:var(--muted); font-size:14px; font-weight:600; cursor:pointer; }
  .seg button.on { background:var(--text); color:var(--bg); border-color:transparent; }

  /* zone order */
  .zone-edit { display:flex; gap:8px; align-items:flex-end; }
  .zone-edit .field { flex:1; min-width:0; }
  .move { display:flex; gap:6px; }
  .move button { width:44px; height:44px; border-radius:12px; border:1px solid var(--line); background:var(--raised);
                 color:var(--text); font-size:18px; cursor:pointer; }
  .move button:disabled { opacity:.3; cursor:default; }
  .zlabel { display:flex; justify-content:space-between; align-items:center; gap:10px; }
  .showtog { display:inline-flex; align-items:center; gap:8px; font-size:13px; color:var(--muted); }
  .switch.small { width:40px; height:24px; }
  .switch.small .slider:before { width:17px; height:17px; left:2.5px; top:2.5px; }
  .switch.small input:checked + .slider:before { transform:translateX(16px); }
  .zone-edit.is-hidden .zname { opacity:.45; }
  .zone-edit { flex-wrap:wrap; }
  .offline-line { flex-basis:100%; display:flex; justify-content:space-between; align-items:center; gap:10px;
                  font-size:13px; color:var(--err); margin-top:2px; }
  .note a { color:var(--text); text-decoration:underline; text-underline-offset:3px; }
</style>
</head>
<body>
<header><h1>Thermostat</h1><span id="status">Loading…</span></header>
<nav><button id="tabZones" class="active">Zones</button><button id="tabPresets">Presets</button>
<button id="tabSched">Schedule</button><button id="tabSettings">Settings</button></nav>

<!-- ============ ZONES ============ -->
<main id="zonesTab">
  <div class="chips" id="presetChips"></div>
  <div class="list" id="zones"><div class="empty">Connecting to Airzone…</div></div>
</main>

<!-- ============ PRESETS ============ -->
<main id="presetsTab" hidden>
  <div class="list" id="presetList"></div>
  <button class="btn primary wide" id="addPresetBtn">+ Add preset</button>
  <div class="card sched" id="presetForm" hidden>
    <div class="name" id="presetFormTitle">New preset</div>
    <div class="field"><label>Name</label><input type="text" id="pName" maxlength="40" placeholder="e.g. Movie night"></div>
    <div class="field"><label>What it does (lines for "All zones" run first, so a single zone can override them)</label>
      <div id="pRules"></div>
      <button class="btn" id="addRuleBtn" style="margin-top:10px">+ Add line</button>
    </div>
    <div class="formErr" id="presetErr"></div>
    <div class="actions"><button class="btn primary" id="savePresetBtn">Save</button><button class="btn" id="cancelPresetBtn">Cancel</button></div>
  </div>
  <div class="note">Tap a preset on the Zones screen to apply it, or use it in a schedule.</div>
</main>

<!-- ============ SCHEDULE ============ -->
<main id="schedTab" hidden>
  <div class="list" id="schedList"></div>
  <button class="btn primary wide" id="addBtn">+ Add schedule</button>

  <div class="card sched" id="form" hidden>
    <div class="name" id="formTitle">New schedule</div>
    <div class="field"><label>Time</label><input type="time" id="fTime" value="07:00"></div>
    <div class="field"><label>Days</label>
      <div class="days" id="fDays"></div>
      <div class="presets"><a data-p="0,1,2,3,4">Weekdays</a><a data-p="5,6">Weekend</a><a data-p="0,1,2,3,4,5,6">Every day</a></div>
    </div>
    <div class="field"><label>Action</label>
      <select id="fAction">
        <option value="preset">Apply a preset</option>
        <option value="on">Turn on</option>
        <option value="off">Turn off</option>
        <option value="setpoint">Change temperature only</option>
      </select></div>
    <div class="field" id="presetField"><label>Preset</label><select id="fPreset"></select></div>
    <div class="field" id="zoneField"><label>Zone</label><select id="fZone"></select></div>
    <div class="field" id="tempField"><label id="tempLabel">Temperature</label>
      <input type="number" id="fTemp" step="0.5" min="5" max="35" value="21"></div>
    <div class="formErr" id="formErr"></div>
    <div class="actions"><button class="btn primary" id="saveBtn">Save</button><button class="btn" id="cancelBtn">Cancel</button></div>
  </div>

  <h2>Recent activity</h2>
  <div class="card sched"><ul class="log" id="log"></ul></div>
  <div class="note" id="clockNote"></div>
</main>

<!-- ============ SETTINGS ============ -->
<main id="settingsTab" hidden>
  <div class="card sched">
    <div class="name">Appearance</div>
    <div class="hint">Saved on this device only. Automatic follows your phone's light or dark setting.</div>
    <div class="seg" id="modeSeg"><button data-m="auto">Automatic</button><button data-m="light">Light</button><button data-m="dark">Dark</button></div>
  </div>
  <div class="card sched">
    <div class="name">Open-window detection</div>
    <div class="hint">When a room's temperature moves fast the wrong way (falling while heating, or rising while cooling), the app assumes a window is open, turns that zone off for a while, then puts it back as it was.</div>
    <div class="togglerow"><span>Enabled</span>
      <label class="switch"><input type="checkbox" id="wEnabled"><span class="slider"></span></label></div>
    <div class="grid3">
      <div class="field"><label>Change of (°)</label><input type="number" id="wDrop" step="0.1" min="0.3" max="5"></div>
      <div class="field"><label>Within (min)</label><input type="number" id="wMinutes" step="1" min="2" max="60"></div>
      <div class="field"><label>Pause (min)</label><input type="number" id="wPause" step="5" min="5" max="120"></div>
    </div>
    <div class="field"><label>Zones watched, with their live temperature drop</label><div id="wZones"></div></div>
    <div class="hint">Only zones that are on and in Heating or Cooling mode are watched (not Auto, Fan or Dry). Tip: open a window for a few minutes with detection off and watch the live change, then set the threshold a little below what you saw. Summer and winter may need different values.</div>
    <div class="formErr" id="wErr"></div>
    <div class="actions"><button class="btn primary" id="wSave">Save</button></div>
  </div>

  <div class="card sched">
    <div class="name">Zones</div>
    <div class="hint">Set the order zones appear in with the arrows, turn off Show to hide a zone from the Zones screen, and rename zones if you like. Names are shown in this app only; leave one empty to use the name from Airzone.</div>
    <div id="nameFields"></div>
    <div class="formErr" id="nErr"></div>
    <div class="actions"><button class="btn primary" id="nSave">Save</button></div>
  </div>

  <div class="card sched">
    <div class="name">Thermostat connection</div>
    <div class="grid2">
      <div class="field"><label>IP address</label><input type="text" id="cIp" inputmode="decimal"></div>
      <div class="field"><label>Port</label><input type="number" id="cPort" min="1" max="65535"></div>
    </div>
    <div class="formErr" id="cErr"></div>
    <div class="actions"><button class="btn" id="cTest">Test</button><button class="btn primary" id="cSave">Save</button></div>
  </div>

  <div class="card sched">
    <div class="name">Backup</div>
    <div class="hint">Download your settings, presets and schedules as one file, and restore them later (on this server or a new one).</div>
    <div class="actions"><button class="btn primary" id="bDown">Download backup</button>
      <button class="btn" id="bUp">Restore from file…</button>
      <input type="file" id="bFile" accept=".json,application/json" hidden></div>
    <div class="formErr" id="bErr"></div>
  </div>
</main>

<script>
const MODES = {1:"Stop",2:"Cooling",3:"Heating",4:"Fan",5:"Dry",7:"Auto"};
const DAYS = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"];
const ACTIONS = {on:"Turn on", off:"Turn off", setpoint:"Set temperature"};
let zones = [], boosts = {}, windows = {}, presets = [], schedules = [], settings = null, gatewayDown = false;
const skippedText = s => s && s.length ? " (skipped, offline: " + s.join(", ") + ")" : "";
let editingId = null, editingPresetId = null;
let formDays = new Set([0,1,2,3,4]);
let formRules = [];
const boostOpen = {};     // zone key -> {setpoint} while the boost panel is open
const pending = {};       // debounce timers per zone
const editing = {};       // zones the user is adjusting right now (don't overwrite)

const $ = id => document.getElementById(id);
const key = z => z.systemID + "-" + z.zoneID;
const unit = z => z.units === 1 ? "°F" : "°C";
const fmt = v => (Math.round(v * 10) / 10).toFixed(1);
const esc = s => String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const zoneName = z => z.name || ("Zone " + z.zoneID);
const busy = () => Object.keys(editing).length || Object.keys(boostOpen).length;

function setStatus(text, err) {
  const s = $("status"); s.textContent = text; s.className = err ? "err" : "";
}

async function api(path, body) {
  const r = await fetch(path, body === undefined ? {} :
    {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(body)});
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}

/* ---------- tabs ---------- */
const TABS = {zones:["zonesTab","tabZones"], presets:["presetsTab","tabPresets"],
              schedule:["schedTab","tabSched"], settings:["settingsTab","tabSettings"]};
function showTab(name) {
  for (const [n, [main, btn]] of Object.entries(TABS)) {
    $(main).hidden = n !== name;
    $(btn).classList.toggle("active", n === name);
  }
  if (name === "zones") load();
  if (name === "schedule") loadSchedules();
  if (name === "presets") loadPresets();
  if (name === "settings") loadSettings(true);
}
$("tabZones").onclick = () => showTab("zones");
$("tabPresets").onclick = () => showTab("presets");
$("tabSched").onclick = () => showTab("schedule");
$("tabSettings").onclick = () => showTab("settings");

function zoneOptions(selected) {
  return '<option value="all"' + (selected === "all" ? " selected" : "") + '>All zones</option>' +
    zones.filter(z => !z.hidden || key(z) === selected)   // hidden zones stay listed only where already used
      .map(z => '<option value="' + key(z) + '"' + (key(z) === selected ? " selected" : "") + '>' + esc(zoneName(z)) + '</option>').join("");
}

/* ---------- zones ---------- */
async function load() {
  try {
    const j = await api("/api/zones");
    zones = j.zones; boosts = j.boosts || {}; windows = j.windows || {}; gatewayDown = !!j.gatewayError;
    renderZones();
    fillZoneSelect();
    if (gatewayDown) setStatus("Thermostat unreachable", true);
    else setStatus("Updated " + new Date().toLocaleTimeString([], {hour:"2-digit", minute:"2-digit"}));
  } catch (e) {
    setStatus("Offline", true);
    if (!zones.length) $("zones").innerHTML =
      '<div class="empty">Could not reach the thermostat.<br><small>' + esc(e.message) + '</small></div>';
  }
}

function renderZones() {
  const root = $("zones");
  if (!zones.length) { root.innerHTML = '<div class="empty">No zones found.</div>'; return; }
  root.innerHTML = "";
  const visible = zones.filter(z => !z.hidden), hiddenCount = zones.length - visible.length;
  if (!visible.length) root.innerHTML = '<div class="empty">All zones are hidden. Show them again in Settings → Zones.</div>';
  visible.forEach(z => {
    const k = key(z), b = boosts[k], w = windows[k], open = boostOpen[k], off = !!z.offline;
    const dis = off ? " disabled" : "";
    const cls = off ? "offline" : w ? "paused" : b ? "boosted" : !z.on ? "off" : z.mode === 3 ? "heat" : z.mode === 2 ? "cool" : "";
    const card = document.createElement("div");
    card.className = "card " + cls;
    const hum = (z.humidity != null && z.humidity > 0) ? ", " + z.humidity + "% humidity" : "";
    const stateText = off ? "Offline, last seen " + esc(z.lastSeen || "unknown")
      : w ? "Paused, window open?" : b ? "Boost" : !z.on ? "Off" : (MODES[z.mode] || "On");
    const modeSelect = Array.isArray(z.modes) && z.modes.length
      ? '<select class="mode"' + dis + '>' + z.modes.map(m =>
          '<option value="' + m + '"' + (m === z.mode ? " selected" : "") + '>' + (MODES[m] || "Mode " + m) + '</option>').join("") + '</select>'
      : (z.mode ? '<div class="now">Mode: ' + (MODES[z.mode] || z.mode) + '</div>' : "");
    let extra;
    if (off) extra = '<div class="hint">' + (gatewayDown
      ? "Can't reach the thermostat right now. Showing the last readings."
      : "This zone isn't answering. Its thermostat may need new batteries or a better signal. Controls come back when it reconnects.") + '</div>';
    else if (w) extra = '<div class="banner window"><span>Window open? ' + (w.direction === "cool" ? "Cooling" : "Heating") +
                   ' paused until ' + esc(w.until) + ' (' + (w.direction === "cool" ? "+" : "−") + fmt(w.drop) +
                   '° detected)</span><button class="btn" data-a="resume">Resume now</button></div>';
    else if (b) extra = '<div class="banner boost"><span>Boost ' + fmt(b.setpoint) + '° until ' + esc(b.until) + ' (' + b.minutesLeft + ' min left)</span>' +
                   '<button class="btn" data-a="cancelboost">Stop</button></div>';
    else if (open) extra = '<div class="boostpanel"><div class="row"><button class="step" data-bd="-1">−</button>' +
          '<div class="btemp">' + fmt(open.setpoint) + '<small style="font-size:14px;color:var(--muted)">' + unit(z) + '</small></div>' +
          '<button class="step" data-bd="1">+</button></div>' +
          '<div class="durations"><button data-min="30">30 min</button><button data-min="60">1 h</button>' +
          '<button data-min="120">2 h</button><button data-min="180">3 h</button></div>' +
          '<div class="hint center">Pick a duration to start. Afterwards the zone goes back to how it is now.</div>' +
          '<div class="actions" style="justify-content:center"><button class="btn" data-a="closeboost">Cancel</button></div></div>';
    else extra = '<div class="actions"><button class="btn boostbtn" data-a="openboost">Boost</button></div>';
    card.innerHTML =
      '<div class="top"><div><div class="name">' + esc(zoneName(z)) + '</div>' +
      '<div class="state">' + stateText + '</div>' +
      '<div class="now">' + (off ? "Last reading " : "Room ") + fmt(z.roomTemp) + unit(z) + hum + '</div></div>' +
      '<label class="switch"><input type="checkbox" class="power" ' + (z.on ? "checked" : "") + dis + '><span class="slider"></span></label></div>' +
      '<div class="set"><button data-d="-1" aria-label="Lower"' + dis + '>−</button>' +
      '<div class="target">' + fmt(z.setpoint) + '<small>' + unit(z) + '</small></div>' +
      '<button data-d="1" aria-label="Raise"' + dis + '>+</button></div>' + modeSelect + extra;

    card.querySelector(".power").onchange = e => send(z, {on: e.target.checked ? 1 : 0});
    card.querySelectorAll(".set button").forEach(btn => btn.onclick = () => {
      const step = z.units === 1 ? 1 : 0.5;
      const min = z.minTemp ?? 15, max = z.maxTemp ?? 30;
      z.setpoint = Math.min(max, Math.max(min, z.setpoint + step * +btn.dataset.d));
      card.querySelector(".target").innerHTML = fmt(z.setpoint) + "<small>" + unit(z) + "</small>";
      editing[k] = true;
      clearTimeout(pending[k]);
      pending[k] = setTimeout(() => { delete editing[k]; send(z, {setpoint: z.setpoint}); }, 800);
    });
    const sel = card.querySelector("select.mode");
    if (sel) sel.onchange = e => send(z, {mode: +e.target.value});

    const on = (s, fn) => { const el = card.querySelector(s); if (el) el.onclick = fn; };
    on('[data-a="openboost"]', () => {
      const max = z.maxTemp ?? 30;
      boostOpen[k] = {setpoint: Math.round(Math.min(max, Math.max(z.setpoint, z.roomTemp) + 2) * 2) / 2};
      renderZones();
    });
    on('[data-a="closeboost"]', () => { delete boostOpen[k]; renderZones(); });
    on('[data-a="cancelboost"]', () => post("/api/boost/cancel", {key: k}, "Stopping boost…", "Boost stopped"));
    on('[data-a="resume"]', () => post("/api/window/resume", {key: k}, "Resuming…", "Heating resumed"));
    card.querySelectorAll("[data-bd]").forEach(btn => btn.onclick = () => {
      const step = z.units === 1 ? 1 : 0.5, min = z.minTemp ?? 15, max = z.maxTemp ?? 30;
      boostOpen[k].setpoint = Math.min(max, Math.max(min, boostOpen[k].setpoint + step * +btn.dataset.bd));
      renderZones();
    });
    card.querySelectorAll("[data-min]").forEach(btn => btn.onclick = () => {
      const sp = boostOpen[k].setpoint;
      delete boostOpen[k];
      post("/api/boost", {key: k, setpoint: sp, minutes: +btn.dataset.min}, "Starting boost…", "Boost started");
    });
    root.appendChild(card);
  });
  if (hiddenCount && visible.length) {
    const note = document.createElement("div");
    note.className = "note";
    note.innerHTML = (hiddenCount === 1 ? "1 zone is hidden. " : hiddenCount + " zones are hidden. ") +
      '<a href="#" id="showHiddenLink">Manage in Settings</a>';
    root.appendChild(note);
    $("showHiddenLink").onclick = e => { e.preventDefault(); showTab("settings"); };
  }
}

async function post(path, body, doing, done) {
  setStatus(doing);
  try { await api(path, body); setStatus(done); } catch (e) { setStatus("Error: " + e.message, true); }
  load();
}

async function send(z, change) {
  setStatus("Saving…");
  try {
    await api("/api/zone", {systemID: z.systemID, zoneID: z.zoneID, ...change});
    setStatus("Saved");
  } catch (e) { setStatus("Error: " + e.message, true); }
  setTimeout(load, 1000);
}

/* ---------- presets ---------- */
function ruleText(r) {
  const t = r.setpoint != null ? fmt(r.setpoint) + "°" : "";
  if (r.action === "off") return r.zoneName + ": turn off";
  if (r.action === "on") return r.zoneName + ": turn on" + (t ? " at " + t : "");
  return r.zoneName + ": set to " + t;
}

async function loadPresets() {
  try {
    presets = (await api("/api/presets")).presets;
    renderPresetChips(); renderPresetList(); fillPresetSelect();
  } catch (e) { setStatus("Error: " + e.message, true); }
}

async function applyPreset(p) {
  setStatus("Applying " + p.name + "…");
  try { const j = await api("/api/presets/apply", {id: p.id}); setStatus(p.name + " applied" + skippedText(j.skipped)); }
  catch (e) { setStatus("Error: " + e.message, true); }
  setTimeout(load, 800);
}

function renderPresetChips() {
  const root = $("presetChips");
  root.innerHTML = "";
  presets.forEach(p => {
    const b = document.createElement("button");
    b.textContent = p.name;
    b.onclick = () => applyPreset(p);
    root.appendChild(b);
  });
}

function renderPresetList() {
  const root = $("presetList");
  if (!presets.length) { root.innerHTML = '<div class="empty">No presets yet.</div>'; return; }
  root.innerHTML = "";
  presets.forEach(p => {
    const card = document.createElement("div");
    card.className = "card sched";
    card.innerHTML = '<div class="name">' + esc(p.name) + '</div>' +
      '<ul class="rules">' + p.rules.map(r => '<li>' + esc(ruleText(r)) + '</li>').join("") + '</ul>' +
      '<div class="actions"><button class="btn primary" data-a="apply">Apply</button>' +
      '<button class="btn" data-a="edit">Edit</button><button class="btn danger" data-a="del">Delete</button></div>';
    card.querySelector('[data-a="apply"]').onclick = () => applyPreset(p);
    card.querySelector('[data-a="edit"]').onclick = () => openPresetForm(p);
    card.querySelector('[data-a="del"]').onclick = async () => {
      if (!confirm('Delete the preset "' + p.name + '"?')) return;
      try { await api("/api/presets/delete", {id: p.id}); } catch (e) { alert(e.message); }
      loadPresets();
    };
    root.appendChild(card);
  });
}

function renderRules() {
  const root = $("pRules");
  root.innerHTML = "";
  formRules.forEach((r, i) => {
    const row = document.createElement("div");
    row.className = "rule";
    row.innerHTML = '<select data-f="zone">' + zoneOptions(r.zone) + '</select>' +
      '<select data-f="action">' + Object.entries(ACTIONS).map(([v, t]) =>
        '<option value="' + v + '"' + (v === r.action ? " selected" : "") + '>' + t + '</option>').join("") + '</select>' +
      '<input data-f="setpoint" type="number" step="0.5" min="5" max="35" placeholder="°" value="' + (r.setpoint ?? "") + '"' +
      (r.action === "off" ? " disabled" : "") + '>' +
      '<button class="x" title="Remove">×</button>';
    row.querySelectorAll("[data-f]").forEach(el => el.onchange = () => {
      r[el.dataset.f] = el.value;
      if (el.dataset.f === "action") renderRules();
    });
    row.querySelector(".x").onclick = () => { formRules.splice(i, 1); renderRules(); };
    root.appendChild(row);
  });
}

function openPresetForm(p) {
  editingPresetId = p ? p.id : null;
  $("presetFormTitle").textContent = p ? "Edit preset" : "New preset";
  $("pName").value = p ? p.name : "";
  formRules = p ? p.rules.map(r => ({...r})) : [{zone:"all", action:"on", setpoint:21}];
  $("presetErr").textContent = "";
  renderRules();
  $("presetForm").hidden = false; $("addPresetBtn").hidden = true;
  $("presetForm").scrollIntoView({behavior:"smooth", block:"start"});
}
function closePresetForm() { $("presetForm").hidden = true; $("addPresetBtn").hidden = false; }
$("addPresetBtn").onclick = () => openPresetForm(null);
$("cancelPresetBtn").onclick = closePresetForm;
$("addRuleBtn").onclick = () => { formRules.push({zone: zones[0] ? key(zones[0]) : "all", action:"setpoint", setpoint:20}); renderRules(); };
$("savePresetBtn").onclick = async () => {
  const names = Object.fromEntries([["all","All zones"], ...zones.map(z => [key(z), zoneName(z)])]);
  const body = {id: editingPresetId, name: $("pName").value,
    rules: formRules.map(r => ({...r, zoneName: names[r.zone] || r.zoneName}))};
  try { await api("/api/presets", body); closePresetForm(); loadPresets(); }
  catch (e) { $("presetErr").textContent = e.message; }
};

/* ---------- schedules ---------- */
function daysText(days) {
  const d = days.join(",");
  if (d === "0,1,2,3,4,5,6") return "Every day";
  if (d === "0,1,2,3,4") return "Weekdays";
  if (d === "5,6") return "Weekend";
  return days.map(i => DAYS[i]).join(", ");
}
function schedText(s) {
  if (s.action === "preset") {
    const p = presets.find(x => x.id === s.presetId);
    return "Preset: " + (p ? p.name : "(deleted)");
  }
  return ruleText(s);
}

async function loadSchedules() {
  try {
    if (!presets.length) presets = (await api("/api/presets")).presets;
    const j = await api("/api/schedules");
    schedules = j.schedules;
    renderSchedules();
    $("log").innerHTML = j.activity.length
      ? j.activity.map(a => '<li class="' + (a.ok ? "" : "fail") + '"><span>' + esc(a.at) + '</span>' + esc(a.text) + '</li>').join("")
      : '<li><span>Nothing has run yet.</span></li>';
    $("clockNote").textContent = "Schedules, boosts and pauses follow the clock of the server (now " + j.now + ").";
  } catch (e) { setStatus("Error: " + e.message, true); }
}

function renderSchedules() {
  const root = $("schedList");
  if (!schedules.length) { root.innerHTML = '<div class="empty">No schedules yet.</div>'; return; }
  root.innerHTML = "";
  schedules.forEach(s => {
    const card = document.createElement("div");
    card.className = "card sched" + (s.enabled ? "" : " disabled");
    card.innerHTML =
      '<div class="top"><div><div class="stime">' + esc(s.time) + '</div>' +
      '<div class="now">' + esc(daysText(s.days)) + '</div>' +
      '<div class="sdesc">' + esc(schedText(s)) + '</div></div>' +
      '<label class="switch"><input type="checkbox" ' + (s.enabled ? "checked" : "") + '><span class="slider"></span></label></div>' +
      '<div class="actions"><button class="btn" data-a="edit">Edit</button>' +
      '<button class="btn" data-a="run">Run now</button>' +
      '<button class="btn danger" data-a="del">Delete</button></div>';
    card.querySelector("input").onchange = async e => {
      try { await api("/api/schedules", {...s, enabled: e.target.checked}); } catch (err) { setStatus("Error: " + err.message, true); }
      loadSchedules();
    };
    card.querySelector('[data-a="edit"]').onclick = () => openForm(s);
    card.querySelector('[data-a="run"]').onclick = async () => {
      setStatus("Running…");
      try { const j = await api("/api/schedules/run", {id: s.id}); setStatus("Done" + skippedText(j.skipped)); }
      catch (err) { setStatus("Error: " + err.message, true); }
      loadSchedules(); setTimeout(load, 1000);
    };
    card.querySelector('[data-a="del"]').onclick = async () => {
      if (!confirm("Delete this schedule?")) return;
      try { await api("/api/schedules/delete", {id: s.id}); } catch (err) { setStatus("Error: " + err.message, true); }
      loadSchedules();
    };
    root.appendChild(card);
  });
}

function fillZoneSelect() {
  const sel = $("fZone"), cur = sel.value || "all";
  sel.innerHTML = zoneOptions(cur);
}
function fillPresetSelect() {
  const sel = $("fPreset"), cur = sel.value;
  sel.innerHTML = presets.map(p => '<option value="' + esc(p.id) + '">' + esc(p.name) + '</option>').join("");
  if ([...sel.options].some(o => o.value === cur)) sel.value = cur;
}

function renderDays() {
  $("fDays").innerHTML = DAYS.map((d, i) =>
    '<button type="button" data-i="' + i + '" class="' + (formDays.has(i) ? "on" : "") + '">' + d + '</button>').join("");
  $("fDays").querySelectorAll("button").forEach(b => b.onclick = () => {
    const i = +b.dataset.i; formDays.has(i) ? formDays.delete(i) : formDays.add(i); renderDays();
  });
}
document.querySelectorAll(".presets a").forEach(a => a.onclick = () => {
  formDays = new Set(a.dataset.p.split(",").map(Number)); renderDays();
});

function updateFormFields() {
  const a = $("fAction").value;
  $("presetField").hidden = a !== "preset";
  $("zoneField").hidden = a === "preset";
  $("tempField").hidden = a === "off" || a === "preset";
  $("tempLabel").textContent = a === "on" ? "Temperature (leave empty to keep current)" : "Temperature";
}
$("fAction").onchange = updateFormFields;

function openForm(s) {
  editingId = s ? s.id : null;
  $("formTitle").textContent = s ? "Edit schedule" : "New schedule";
  $("fTime").value = s ? s.time : "07:00";
  formDays = new Set(s ? s.days : [0,1,2,3,4]);
  fillZoneSelect(); fillPresetSelect();
  $("fAction").value = s ? s.action : (presets.length ? "preset" : "on");
  $("fZone").value = s && s.action !== "preset" ? s.zone : "all";
  if (s && s.presetId) $("fPreset").value = s.presetId;
  $("fTemp").value = s ? (s.setpoint ?? "") : "21";
  $("formErr").textContent = "";
  renderDays(); updateFormFields();
  $("form").hidden = false; $("addBtn").hidden = true;
  $("form").scrollIntoView({behavior:"smooth", block:"start"});
}
function closeForm() { $("form").hidden = true; $("addBtn").hidden = false; }
$("addBtn").onclick = () => openForm(null);
$("cancelBtn").onclick = closeForm;
$("saveBtn").onclick = async () => {
  const zoneSel = $("fZone");
  const old = schedules.find(x => x.id === editingId);
  const body = {
    id: editingId, enabled: old ? old.enabled : true,
    time: $("fTime").value, days: [...formDays].sort(),
    action: $("fAction").value, presetId: $("fPreset").value,
    zone: zoneSel.value, zoneName: zoneSel.options[zoneSel.selectedIndex]?.text || "All zones",
    setpoint: $("fTemp").value,
  };
  try { await api("/api/schedules", body); closeForm(); loadSchedules(); }
  catch (e) { $("formErr").textContent = e.message; }
};

/* ---------- settings ---------- */
let settingsZones = [];
function msg(id, text, ok) { const el = $(id); el.textContent = text; el.className = ok ? "formOk" : "formErr"; }

async function loadSettings(fillForms) {
  try {
    const j = await api("/api/settings");
    settings = j.settings;
    if (j.zones.length) settingsZones = j.zones;
    if (fillForms) {
      const w = settings.window;
      $("wEnabled").checked = w.enabled;
      $("wDrop").value = w.drop; $("wMinutes").value = w.minutes; $("wPause").value = w.pause;
      $("cIp").value = settings.airzoneIp; $("cPort").value = settings.airzonePort;
      renderZoneEditor();
      ["wErr","nErr","cErr","bErr"].forEach(id => msg(id, ""));
    }
    renderWatchZones(j.monitor, j.windows || {}, fillForms);
    if (j.zonesError && fillForms) msg("cErr", "Can't reach the thermostat right now: " + j.zonesError);
  } catch (e) { setStatus("Error: " + e.message, true); }
}

function renderWatchZones(monitor, paused, fillForms) {
  const root = $("wZones");
  const keep = {};
  root.querySelectorAll("input[data-k]").forEach(i => keep[i.dataset.k] = i.checked);
  if (!settingsZones.length) { root.innerHTML = '<div class="hint">Zones appear here once the thermostat is reachable.</div>'; return; }
  const threshold = settings.window.drop;
  root.innerHTML = settingsZones.map(z => {
    const m = monitor[z.key];
    let live = "Not heating or cooling right now";
    let hot = false;
    if (z.offline) live = "Offline, last seen " + esc(z.lastSeen || "unknown");
    else if (paused[z.key]) { live = "Paused for an open window until " + paused[z.key].until; hot = true; }
    else if (m && m.drop == null) live = (m.direction === "cool" ? "Cooling" : "Heating") + ", collecting readings…";
    else if (m) {
      const cool = m.direction === "cool";
      const over = " over the last " + (m.minutes >= 2 ? m.minutes + " min" : "minute");
      live = (cool ? "Cooling. Live: " : "Heating. Live: ") +
        (m.drop > 0 ? (cool ? "+" : "−") + fmt(m.drop) + "°" : (cool ? "no rise" : "no drop")) + over;
      hot = m.drop >= threshold * 0.7;
    }
    const checked = fillForms ? (settings.window.zones[z.key] !== false) : keep[z.key];
    return '<div class="zrow"><div><div>' + esc(z.name) + '</div><div class="live' + (hot ? " hot" : "") + '">' + live + '</div></div>' +
      '<label class="switch"><input type="checkbox" data-k="' + esc(z.key) + '"' + (checked ? " checked" : "") + '><span class="slider"></span></label></div>';
  }).join("");
}

$("wSave").onclick = async () => {
  const zonesOn = {};
  $("wZones").querySelectorAll("input[data-k]").forEach(i => zonesOn[i.dataset.k] = i.checked);
  try {
    await api("/api/settings", {window: {enabled: $("wEnabled").checked, drop: $("wDrop").value,
      minutes: $("wMinutes").value, pause: $("wPause").value, zones: zonesOn}});
    msg("wErr", "Saved", true); loadSettings(false);
  } catch (e) { msg("wErr", e.message); }
};
function renderZoneEditor() {
  const root = $("nameFields");
  if (!settingsZones.length) { root.innerHTML = '<div class="hint">Zones appear here once the thermostat is reachable.</div>'; return; }
  root.innerHTML = settingsZones.map(z =>
    '<div class="zone-edit' + (z.hidden ? " is-hidden" : "") + '" data-k="' + esc(z.key) + '"><div class="field">' +
    '<div class="zlabel"><label>' + esc(z.airzoneName) + '</label>' +
    '<span class="showtog">Show <label class="switch small"><input type="checkbox" class="show" aria-label="Show ' + esc(z.name) + '"' +
    (z.hidden ? "" : " checked") + '><span class="slider"></span></label></span></div>' +
    '<input type="text" class="zname" maxlength="40" placeholder="' + esc(z.airzoneName) + '" value="' + esc(settings.zoneNames[z.key] || "") + '"></div>' +
    '<div class="move"><button data-mv="-1" aria-label="Move ' + esc(z.name) + ' up">↑</button>' +
    '<button data-mv="1" aria-label="Move ' + esc(z.name) + ' down">↓</button></div>' +
    (z.offline ? '<div class="offline-line"><span>Offline, last seen ' + esc(z.lastSeen || "unknown") + '</span>' +
      '<button class="btn" data-forget="' + esc(z.key) + '">Forget this zone</button></div>' : "") +
    '</div>').join("");
  root.querySelectorAll("[data-forget]").forEach(btn => btn.onclick = async () => {
    const z = settingsZones.find(x => x.key === btn.dataset.forget);
    if (!confirm('Forget "' + (z ? z.name : "this zone") + '"? It disappears from the app until it comes back online.')) return;
    try { await api("/api/zones/forget", {key: btn.dataset.forget}); settingsZones = []; await loadSettings(true); load(); msg("nErr", "Zone forgotten", true); }
    catch (e) { msg("nErr", e.message); }
  });
  root.querySelectorAll("[data-mv]").forEach(btn => btn.onclick = () => {
    const row = btn.closest(".zone-edit");
    if (+btn.dataset.mv < 0 && row.previousElementSibling) row.parentNode.insertBefore(row, row.previousElementSibling);
    if (+btn.dataset.mv > 0 && row.nextElementSibling) row.parentNode.insertBefore(row.nextElementSibling, row);
    markMoveButtons();
    btn.focus();
    msg("nErr", "Tap Save to keep this order", true);
  });
  root.querySelectorAll("input.show").forEach(cb => cb.onchange = () => {
    cb.closest(".zone-edit").classList.toggle("is-hidden", !cb.checked);
    msg("nErr", "Tap Save to apply", true);
  });
  markMoveButtons();
}
function markMoveButtons() {
  const rows = [...$("nameFields").querySelectorAll(".zone-edit")];
  rows.forEach((r, i) => {
    r.querySelector('[data-mv="-1"]').disabled = i === 0;
    r.querySelector('[data-mv="1"]').disabled = i === rows.length - 1;
  });
}
$("nSave").onclick = async () => {
  const names = {}, order = [], hidden = [];
  $("nameFields").querySelectorAll(".zone-edit").forEach(r => {
    order.push(r.dataset.k);
    names[r.dataset.k] = r.querySelector("input.zname").value;
    if (!r.querySelector("input.show").checked) hidden.push(r.dataset.k);
  });
  try {
    await api("/api/settings", {zoneNames: names, zoneOrder: order, hiddenZones: hidden});
    await loadSettings(true); msg("nErr", "Saved", true); load();
  }
  catch (e) { msg("nErr", e.message); }
};
$("cTest").onclick = async () => {
  msg("cErr", "Testing…", true);
  try { const j = await api("/api/settings/test", {airzoneIp: $("cIp").value, airzonePort: $("cPort").value});
        msg("cErr", "Connected: " + j.zones + " zone(s) found", true); }
  catch (e) { msg("cErr", "No answer: " + e.message); }
};
$("cSave").onclick = async () => {
  try { await api("/api/settings", {airzoneIp: $("cIp").value, airzonePort: $("cPort").value});
        await loadSettings(true); msg("cErr", "Saved", true); load(); }
  catch (e) { msg("cErr", e.message); }
};
$("bDown").onclick = async () => {
  try {
    const r = await fetch("/api/backup");
    if (!r.ok) throw new Error(r.statusText);
    const blob = await r.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "airzone-backup-" + new Date().toISOString().slice(0, 10) + ".json";
    document.body.appendChild(a); a.click(); a.remove();
    msg("bErr", "Backup downloaded", true);
  } catch (e) { msg("bErr", e.message); }
};
$("bUp").onclick = () => $("bFile").click();
$("bFile").onchange = async e => {
  const f = e.target.files[0];
  e.target.value = "";
  if (!f) return;
  if (!confirm("Replace your current settings, presets and schedules with this backup?")) return;
  try {
    const data = JSON.parse(await f.text());
    const j = await api("/api/backup/restore", data);
    msg("bErr", "Restored " + j.presets + " presets and " + j.schedules + " schedules", true);
    loadSettings(true); loadPresets(); load();
  } catch (err) { msg("bErr", err.message); }
};

/* ---------- appearance ---------- */
function markMode() {
  const m = document.documentElement.dataset.mode;
  $("modeSeg").querySelectorAll("button").forEach(b => b.classList.toggle("on", b.dataset.m === m));
}
$("modeSeg").querySelectorAll("button").forEach(b => b.onclick = () => {
  applyMode(b.dataset.m);
  try { localStorage.setItem("mode", b.dataset.m); } catch (e) {}
  markMode();
});
markMode();

/* ---------- start ---------- */
load();
loadPresets();
setInterval(() => {
  if (document.hidden) return;
  if (!$("schedTab").hidden) loadSchedules();
  else if (!$("settingsTab").hidden) loadSettings(false);
  else if (!$("zonesTab").hidden && !busy()) load();
}, 15000);
document.addEventListener("visibilitychange", () => { if (!document.hidden && !busy() && !$("zonesTab").hidden) load(); });
</script>
</body>
</html>"""


# Typefaces served at /fonts/<name>.woff2: Big Shoulders Display and Onest,
# both under the SIL Open Font License 1.1 (via the @fontsource packages).
FONTS = {
    "bsd-300": (
        "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"
        "03lz23PkWwAAAA=="
    ),
    "bsd-400": (
        "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"
        "J5VvR3/gF/mB8u1p7vFNZDsIml3mafXGf1tddpXUG8HBqqq0w6Ldwp5Fy+Z1aMrS5+8tDbnzShyK4ruInAlSSLMaZWEdi2gidU3h"
        "RdrGqOD20vOmZqciRapjfaQ0IXBjSNq8N6fmNRIpokV2vBSYMph5VLv8EThYldZ0MIHTGEXVpsro+aNc+GRhdc3CYHnCgutN2F4i"
        "zhNLVOx6/odyF9FoZys9woQMqMZFRNJbeq4s7Wtxah1ROEDVgtFgqHaiR1vbJ6p64oZ5utoUl3pvkdf8HKUMZ5U/ZM5/+GtoRoA1"
        "xcnfH6QW/4vFO5yJyJgs+VacswJGrMwD/dQ0FmMvs6ySlSfrnIscS/4HmljjHdOjE92jrMXUeNU3qpo+V7Nvft6eBSqisNhskd5K"
        "Wcg5B8lBlAen5fTxipTLIxR7/hJKawWyLvHfjnkoTK7q/iSi1eW+tJ5VBIOoxQyJFO9oIsWiEa8xFDHyY7IyhVGZkFkUdgOYO/a8"
        "c77infOVK/NX5UPOhkci39Y1tcYsYVkexzDSVGLhJ52XLY6slaX65zJbO9uAkvmuU67L4i/XMvfJLPavKOOvzVqr5K8oszggY+5b"
        "XgpPvx/dFaceILX4GrKBEB6uJQ3qkV0Jo6O7EnNHsAEm4N5lOI7lIFalKJXS4D4XN+78xPhsWotZZC/uJVl0aZIqIejTYFtlM5wQ"
        "9PJdddaQj8fgPXgEJlI/WmSeGi0i9eQ1YmtJeGQFqRrNm5oeycVqJs+mydHLjtSoenfErv3T4/s1+HLN/93djrtjNjlM3qDUHuNw"
        "kivLTLzn1DQBTOHEfx73MxPk5cUt1DCW45uX8TTmU4OVbRMNMc9ARIrImcvkfVIP4qcxcTnxqRYpOQmgXj7hyro1Acw9fBNYWJkA"
        "V1StluRP691ILW2ke0iBdU5xiP82/5DiHB4poHoYvaTOtSl/erWkKiuxNDFCp3AhxXQL1Uzy7CqkQ0KuQMiVDlXYkjyqmW4hJS7y"
        "CH1pQiJ4r8hKKkmI0MtdSAnVQjeTPNsK6ZCIKxBtK9hNo5upzsUuighdSWJSFgdbanIldXTvxrwcXbBgvSNGD2UktW76qwG65cC6"
        "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"
        "AA=="
    ),
    "bsd-600": (
        "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"
    ),
    "onest-400": (
        "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"
    ),
    "onest-500": (
        "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"
        "ciEP8qERxC7CiAjwTOBiH8w2UltdsggHwU5k7OOQcRkfx4jjFGd8Rx0TkwlxiSsWNpMSEaJcnhJUPOH/E9v+Hp91Do6JJBMjjVtk"
        "cY88HtzQbBbqssRS8xSbj4winvGyQDeud947SWIiV2uUikbFO8r4RBXfqOMXTbTxT0ACE2S5WMvc98QDDz2NLsEJSWjCEp4IpxOZ"
        "qEQnxq8++y2x+Zh6Irrsc/o7FTWq+qbx8/npsdXa+DDUaIPIjdNfr7OazabGqaVZ29+k3VKKlmNtNMWRr/ktWt9S88831GKMt8di"
        "0/rGmld+7+PqGI3Tp10brWZt1e75BjTxSGymvJVW7FJTU/E2Ve54czL+VM/Y3jwXUfZoyQsYYTZfS7FxOv3lwbcPmXvhj9BED0Y1"
        "qaqnvt8T99rKsSdw7wP9DdJt7WVcpUtqVCt1+PlPRoK1vbrbGnY/3xSJk1mClFRpVUGbJufxmHLgFJGM92lbjL8zobdKLbHy38+7"
        "ILbJFn9nx7VqHT1vEH2FtK3Ty3XVC4vlP9+LSYrGDezfQDRtrXmezH3+B2uCuEWdqvHhHNgwzDH0ni+mVaMqrf/GiqbvdmHWfYxr"
        "YmKdKTFU3fT21GKMlTj2d6rS2PryaRe3pVz4IsXCXPOr4gY/kRnBJzNWDTwIAAAA"
    ),
    "onest-600": (
        "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"
    ),
}


def main():
    try:
        print(f"Airzone device : {base_url()}")
    except RuntimeError:
        print("Airzone device : not set yet (enter it in Settings → Thermostat connection)")
    print(f"Data folder    : {DATA_DIR}")
    print(f"Open the app   : http://localhost:{LISTEN_PORT}")
    print("Press Ctrl+C to stop.")
    threading.Thread(target=scheduler_loop, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
