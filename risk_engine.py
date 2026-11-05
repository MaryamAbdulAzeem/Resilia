"""
Heat-stress risk engine.

Reads the wristband readings in data/readings.jsonl, fetches live weather
for each worker's site from Open-Meteo, estimates WBGT, and gives each
worker a GREEN / YELLOW / RED heat-stress level.

Results are printed and saved to data/risk_scores.json (for a dashboard later).

Run once:            python risk_engine.py
Keep updating:       python risk_engine.py --watch
Without internet:    python risk_engine.py --offline

NOTE: this is a hackathon prototype, not a medical or safety-certified tool.
"""

import argparse
import json
import math
import os
import sys
import time
import urllib.request
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

import simulator  # for the simulated clock, time-of-day sun, and UAE time zone
from agent import Agent
from predictor import HISTORY_READINGS, READINGS_PER_MINUTE, load_predictor

READINGS_FILE = Path("data") / "readings.jsonl"
SCORES_FILE = Path("data") / "risk_scores.json"
# The dashboard's live/offline switch writes here; the engine checks it every cycle.
SETTINGS_FILE = Path("data") / "settings.json"
# Every alert the engine raises (and when it escalates or clears) is logged here.
ALERTS_FILE = Path("data") / "alerts.jsonl"
# An alert stays open until the worker has been calm (not RED, not predicted RED)
# for this long. Stops one worker flickering RED/YELLOW from raising many alerts.
CLEAR_AFTER_SIM_SECONDS = 120

# How many recent readings to average per worker.
# 12 readings x 5 seconds = the last 1 minute. Averaging smooths out sensor noise.
WINDOW = 12

# Map coordinates for each site (needed to ask for the weather).
SITE_COORDS = {
    "Dubai": (25.2048, 55.2708),
    "Abu Dhabi": (24.4539, 54.3773),
    "Sharjah": (25.3463, 55.4209),
    "Al Ain": (24.2075, 55.7447),
}

# Fetch fresh weather at most this often (Open-Meteo updates every 15 minutes).
WEATHER_REFRESH_SECONDS = 600

# The highest WBGT that's reasonably safe for continuous work at each activity level.
# The harder you work, the more heat your body makes, so the lower the limit.
# Roughly based on ACGIH guidance for acclimatised workers.
WBGT_LIMITS = {"resting": 33.0, "light": 31.0, "moderate": 28.0, "heavy": 26.0}


# ---------------------------------------------------------------------------
# Step 1: read the wristband data
# ---------------------------------------------------------------------------

def read_last_lines(path, max_bytes):
    """
    Read only the end of a file. The readings file keeps growing (fast in demo
    mode), but we only ever need the last few minutes of it.
    """
    with path.open("rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        chunk = f.read().decode("utf-8", errors="ignore")
    lines = chunk.splitlines()
    if size > max_bytes:
        lines = lines[1:]  # the first line is probably cut in half
    return lines


def load_recent_readings(history=WINDOW):
    """Return {worker_id: [last `history` readings]} from the readings file."""
    recent = defaultdict(lambda: deque(maxlen=history))  # deque keeps only the newest items
    if not READINGS_FILE.exists():
        return {}
    # Each reading is ~250 bytes; leave plenty of room for 15 workers x `history` readings.
    max_bytes = 15 * history * 250 * 3
    for line in read_last_lines(READINGS_FILE, max_bytes):
        line = line.strip()
        if not line:
            continue
        try:
            reading = json.loads(line)
        except json.JSONDecodeError:
            continue  # skip a half-written line (the simulator may be writing right now)
        recent[reading["worker_id"]].append(reading)
    return {wid: list(r) for wid, r in recent.items()}


# ---------------------------------------------------------------------------
# Step 2: get live weather from Open-Meteo
# ---------------------------------------------------------------------------

def fetch_weather(site):
    """Ask Open-Meteo for the current temperature and humidity at a site."""
    lat, lon = SITE_COORDS[site]
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&current=temperature_2m,relative_humidity_2m,shortwave_radiation"
        "&timezone=Asia/Dubai"
    )
    with urllib.request.urlopen(url, timeout=10) as response:
        data = json.load(response)
    current = data["current"]
    return {
        "air_temp_c": current["temperature_2m"],
        "humidity_pct": current["relative_humidity_2m"],
        "solar_w_m2": current["shortwave_radiation"],  # sunlight strength; 0 at night
        "observed_at": current["time"],
        "source": "open-meteo",
    }


class WeatherCache:
    """Remembers weather per site so we don't call the API every 5 seconds."""

    def __init__(self, offline=False):
        self.offline = offline
        self.cache = {}  # site -> (time fetched, weather)

    def set_offline(self, offline):
        """Switch weather mode. Forget cached weather so the change shows at once."""
        if offline != self.offline:
            self.offline = offline
            self.cache = {}
            print(f"  Weather mode switched to {'OFFLINE summer scenario' if offline else 'LIVE'}.")

    def get(self, site, fallback_temp, sim_hour=None):
        # Summer scenario: weather comes from the simulator (its air temperature
        # follows the simulated time of day), so work it out fresh every time.
        if self.offline:
            return simulated_weather(fallback_temp, sim_hour)

        now = time.time()
        cached = self.cache.get(site)
        if cached and now - cached[0] < WEATHER_REFRESH_SECONDS:
            return cached[1]

        weather = None
        if not self.offline and site in SITE_COORDS:
            try:
                weather = fetch_weather(site)
            except Exception as error:  # no internet, API down, etc.
                print(f"  ! Could not get weather for {site} ({error}). Using simulated values.")

        fetched_at = now
        if weather is None:
            weather = simulated_weather(fallback_temp, sim_hour)
            # Live weather failed: try again in 1 minute, not 10.
            fetched_at = now - WEATHER_REFRESH_SECONDS + 60
        self.cache[site] = (fetched_at, weather)
        return weather


def simulated_weather(air_temp, sim_hour=None):
    """Summer scenario: the simulator's air temperature, dry air (30%), and sun for the time of day."""
    return {
        "air_temp_c": air_temp,
        "humidity_pct": 30,
        "solar_w_m2": round(simulator.sun_w_m2(sim_hour)) if sim_hour is not None else 800,
        "observed_at": None,
        "source": "simulated",
    }


def sun_times(day, summer=False, lat=24.45, lon=54.38):
    """
    Approximate sunrise and sunset (UAE time) for Abu Dhabi, for the heat arc.
    The summer scenario uses midsummer's day. Accurate to about 10 minutes.
    """
    if summer:
        day = day.replace(month=6, day=21)
    doy = day.timetuple().tm_yday
    decl = math.radians(23.44) * math.sin(2 * math.pi * (284 + doy) / 365)
    b = 2 * math.pi * (doy - 81) / 364
    eot_min = 9.87 * math.sin(2 * b) - 7.53 * math.cos(b) - 1.5 * math.sin(b)  # equation of time
    cos_h = (math.sin(math.radians(-0.83)) - math.sin(math.radians(lat)) * math.sin(decl)) / (
        math.cos(math.radians(lat)) * math.cos(decl))
    half_day_h = math.degrees(math.acos(max(-1, min(1, cos_h)))) / 15
    noon_h = 12 + (60 - lon) * 4 / 60 - eot_min / 60   # GST is UTC+4 (60 degrees east)
    fmt = lambda h: f"{int(h):02d}:{int(round((h % 1) * 60)) % 60:02d}"
    return {"sunrise": fmt(noon_h - half_day_h), "sunset": fmt(noon_h + half_day_h)}


# ---------------------------------------------------------------------------
# Step 3: calculate WBGT
# ---------------------------------------------------------------------------

def wet_bulb_temp(air_temp_c, humidity_pct):
    """
    Wet-bulb temperature: how cold a wet cloth gets as water evaporates from it.
    Humid air means less evaporation, so the wet bulb stays close to the air
    temperature. That's why humidity makes heat more dangerous: sweat can't
    cool you. Uses Stull's (2011) formula.
    """
    t, rh = air_temp_c, humidity_pct
    return (
        t * math.atan(0.151977 * math.sqrt(rh + 8.313659))
        + math.atan(t + rh)
        - math.atan(rh - 1.676331)
        + 0.00391838 * rh ** 1.5 * math.atan(0.023101 * rh)
        - 4.686035
    )


def estimate_wbgt(air_temp_c, humidity_pct, solar_w_m2):
    """
    Estimate WBGT (Wet Bulb Globe Temperature), the standard measure of heat stress.

    The official formula, used with special instruments, is:
        WBGT = 0.7 * wet bulb + 0.2 * globe temp + 0.1 * air temp
    "Globe temp" is measured inside a black ball sitting in the sun. We don't
    have one, so we estimate it as air temp + extra heat from sunlight
    (about +13 C in full 1000 W/m2 sun). This is an approximation, and it ignores wind.
    """
    wet_bulb = wet_bulb_temp(air_temp_c, humidity_pct)
    globe = air_temp_c + 0.013 * solar_w_m2
    return 0.7 * wet_bulb + 0.2 * globe + 0.1 * air_temp_c


# ---------------------------------------------------------------------------
# Step 4: score each worker
# ---------------------------------------------------------------------------

def score_worker(readings, weather):
    """
    Give 0, 1 or 2 points for each of three warning signs, then add them up:

      environment : WBGT compared with the safe limit for their activity
      heart rate  : 1-minute average heart rate
      skin temp   : 1-minute average skin temperature

    Total 0-1 = GREEN, 2-3 = YELLOW, 4-6 = RED.
    """
    latest = readings[-1]
    avg_hr = sum(r["heart_rate_bpm"] for r in readings) / len(readings)
    avg_skin = sum(r["skin_temp_c"] for r in readings) / len(readings)

    # Use the most common activity in the window, not just the latest one.
    activities = [r["activity_level"] for r in readings]
    activity = max(set(activities), key=activities.count)

    wbgt = estimate_wbgt(weather["air_temp_c"], weather["humidity_pct"], weather["solar_w_m2"])
    limit = WBGT_LIMITS[activity]

    reasons = []

    # Environment points
    if wbgt > limit:
        env_points = 2
        reasons.append(f"WBGT {wbgt:.1f} above {limit} limit for {activity} activity")
    elif wbgt > limit - 2:
        env_points = 1
        reasons.append(f"WBGT {wbgt:.1f} close to {limit} limit for {activity} activity")
    else:
        env_points = 0

    # Heart-rate points
    if avg_hr > 155:
        hr_points = 2
        reasons.append(f"heart rate very high ({avg_hr:.0f} bpm)")
    elif avg_hr > 130:
        hr_points = 1
        reasons.append(f"heart rate elevated ({avg_hr:.0f} bpm)")
    else:
        hr_points = 0

    # Skin-temperature points
    if avg_skin > 36.5:
        skin_points = 2
        reasons.append(f"skin temperature very high ({avg_skin:.1f} C)")
    elif avg_skin > 35.5:
        skin_points = 1
        reasons.append(f"skin temperature elevated ({avg_skin:.1f} C)")
    else:
        skin_points = 0

    total = env_points + hr_points + skin_points
    if total >= 4:
        level = "RED"
    elif total >= 2:
        level = "YELLOW"
    else:
        level = "GREEN"

    return {
        "worker_id": latest["worker_id"],
        "site": latest["site"],
        "job": latest["job"],
        "level": level,
        "score": total,
        "reasons": reasons,
        "avg_heart_rate_bpm": round(avg_hr),
        "avg_skin_temp_c": round(avg_skin, 1),
        "activity_level": activity,
        "wbgt_c": round(wbgt, 1),
        "wbgt_limit": limit,
        "env_points": env_points,
        "hr_points": hr_points,
        "skin_points": skin_points,
        "air_temp_c": weather["air_temp_c"],
        "humidity_pct": weather["humidity_pct"],
        "solar_w_m2": weather["solar_w_m2"],
        "weather_source": weather["source"],
        "last_reading_at": latest["timestamp"],
    }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

COLOURS = {"GREEN": "\033[92m", "YELLOW": "\033[93m", "RED": "\033[91m"}
RESET = "\033[0m"


def print_report(results):
    use_colour = sys.stdout.isatty()  # only colour when printing to a real terminal
    print(f"\n=== Heat-stress report  ({datetime.now():%H:%M:%S}) ===")
    print(f"{'ID':<4} {'Site':<10} {'Level':<7} {'Pts':>3} {'HR':>4} {'Skin':>5} "
          f"{'WBGT':>5}  Activity  Why")
    for r in results:
        colour = COLOURS[r["level"]] if use_colour else ""
        reset = RESET if use_colour else ""
        why = "; ".join(r["reasons"]) or "-"
        f = r.get("forecast")
        if f:
            why = f"[AI: {f['level']} in ~{f['minutes']} min, {f['probability']:.0%}] " + why
        print(f"{r['worker_id']:<4} {r['site']:<10} {colour}{r['level']:<7}{reset} "
              f"{r['score']:>3} {r['avg_heart_rate_bpm']:>4} {r['avg_skin_temp_c']:>5} "
              f"{r['wbgt_c']:>5}  {r['activity_level']:<9} {why}")

    counts = {lvl: sum(1 for r in results if r["level"] == lvl) for lvl in COLOURS}
    print(f"Summary: {counts['GREEN']} green, {counts['YELLOW']} yellow, {counts['RED']} red")


def crew_status(r):
    """
    The plain-language status shown to supervisors:
      act    "Act now"  - pressed SOS, is RED, or the AI predicts RED
      rising "Rising"   - body signs of strain (heart rate / skin temp) or a rising forecast
      steady "Steady"   - none of the above (it may still be a hot site: see WBGT)
    """
    f = r.get("forecast")
    if r.get("sos_pressed_at"):
        return "act"
    if r["level"] == "RED" or (f and f["level"] == "RED"):
        return "act"
    if f or r["hr_points"] + r["skin_points"] > 0:
        return "rising"
    return "steady"


def trend_5m(history, key):
    """How much a reading rose (+) or fell (-) over the last 5 minutes (1-minute averages)."""
    per_min = READINGS_PER_MINUTE
    if len(history) < per_min * 2:
        return 0.0
    now = history[-per_min:]
    before = history[-HISTORY_READINGS:-HISTORY_READINGS + per_min] if len(history) >= HISTORY_READINGS else history[:per_min]
    avg = lambda rows: sum(r[key] for r in rows) / len(rows)
    return round(avg(now) - avg(before), 1)


class SiteHistory:
    """Keeps one WBGT point per site per simulated minute, for the trend chart."""

    MAX_POINTS = 12 * 60   # up to 12 simulated hours
    SEND_POINTS = 180      # send the last 3 hours to the dashboard

    def __init__(self):
        self.points = defaultdict(list)  # site -> [[ "HH:MM", wbgt, air ], ...]

    def add(self, site, sim_time, wbgt, air):
        if not sim_time:
            return
        minute = sim_time[11:16]  # "HH:MM"
        pts = self.points[site]
        if pts and pts[-1][0] == minute:
            pts[-1] = [minute, wbgt, air]
        else:
            pts.append([minute, wbgt, air])
            del pts[:-self.MAX_POINTS]

    def recent(self, site):
        return self.points[site][-self.SEND_POINTS:]


def device_summary(workers, latest_readings):
    """Helmet connection summary for the 'site gateways' tile."""
    now = datetime.now(timezone.utc)
    ages = []
    for r in latest_readings:
        try:
            ages.append((now - datetime.fromisoformat(r["timestamp"])).total_seconds())
        except (KeyError, ValueError):
            pass
    connected = sum(1 for a in ages if a < 30)
    batteries = [w["battery_pct"] for w in workers if w.get("battery_pct") is not None]
    return {
        "helmets": len(workers),
        "connected": connected,
        "gateways": sorted({w["gateway"] for w in workers if w.get("gateway")}),
        "last_sync_s": round(min(ages)) if ages else None,
        "avg_battery_pct": round(sum(batteries) / len(batteries)) if batteries else None,
        "low_battery": sorted(w["worker_id"] for w in workers if (w.get("battery_pct") or 100) < 20),
    }


def run_once(weather_cache, predictor=None, speed=1, settings_version=0, alert_manager=None,
             site_history=None, agent=None):
    # Keep 6 minutes of history: the last 1 minute is used for today's score,
    # the full 6 minutes lets the AI forecast see trends.
    readings_by_worker = load_recent_readings(history=HISTORY_READINGS)
    if not readings_by_worker:
        print(f"No readings found in {READINGS_FILE}. Start the simulator first: python simulator.py")
        return

    # The simulator's clock (falls back to real time for old readings without one).
    latest_all = [h[-1] for h in readings_by_worker.values()]
    sim_time = max((r.get("sim_time") or "" for r in latest_all), default="") or \
        datetime.now(simulator.GST).isoformat(timespec="seconds")
    sim_dt = datetime.fromisoformat(sim_time)
    sim_hour = simulator.hour_of(sim_dt)

    results = []
    sites = {}
    for worker_id in sorted(readings_by_worker):
        history = readings_by_worker[worker_id]
        latest = history[-1]
        site = latest["site"]
        weather = weather_cache.get(site, latest.get("air_temp_c", 40.0), sim_hour)
        result = score_worker(history[-WINDOW:], weather)
        result["lat"], result["lon"] = SITE_COORDS.get(site, (None, None))

        # Helmet details from the latest reading.
        for key in ("helmet_id", "gateway", "zone", "battery_pct", "sos_pressed_at", "motionless_s", "on_break"):
            result[key] = latest.get(key)
        result["sim_time"] = latest.get("sim_time")
        result["hr_change_5m"] = trend_5m(history, "heart_rate_bpm")
        result["skin_change_5m"] = trend_5m(history, "skin_temp_c")

        # AI forecast: will this worker's risk rise in the next 15 minutes?
        result["forecast"] = None
        if predictor is not None:
            wbgt = estimate_wbgt(weather["air_temp_c"], weather["humidity_pct"], weather["solar_w_m2"])
            result["forecast"] = predictor.forecast(history, wbgt, result["level"], result["score"])
        result["status"] = crew_status(result)
        results.append(result)
        if site_history is not None:
            site_history.add(site, sim_time, result["wbgt_c"], weather["air_temp_c"])
        sites[site] = {
            "lat": result["lat"],
            "lon": result["lon"],
            "air_temp_c": weather["air_temp_c"],
            "humidity_pct": weather["humidity_pct"],
            "solar_w_m2": weather["solar_w_m2"],
            "wbgt_c": result["wbgt_c"],
            "source": weather["source"],
            "observed_at": weather["observed_at"],
            "history": site_history.recent(site) if site_history is not None else [],
        }

    # Most at-risk workers first; among equal scores, rising forecasts first (soonest first).
    def sort_key(r):
        f = r.get("forecast")
        return (-r["score"], 0 if f else 1, f["minutes"] if f else 0, r["worker_id"])
    results.sort(key=sort_key)
    alerts = alert_manager.update(results, speed, sim_time) if alert_manager else []
    # The AI agent acts on the alerts (automatic steps) and asks before big ones.
    agent_summary = agent.step(results, alerts, sites, sim_time) if agent else {"active": False}
    print_report(results)

    mode = "offline" if weather_cache.offline else "live"
    output = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "sim_time": sim_time,                     # the simulator's clock (UAE time)
        "sun": sun_times(sim_dt.date(), summer=(mode == "offline")),
        "devices": device_summary(results, latest_all),
        "weather_mode": mode,
        # Which switch change this result reflects (the dashboard waits for it).
        "settings_version": settings_version,
        # In live mode, sites where Open-Meteo failed and simulated values are used.
        "live_weather_failed": sorted(s for s, v in sites.items()
                                      if mode == "live" and v["source"] != "open-meteo"),
        "speed": speed,
        "agent": agent_summary,
        "alerts": alerts,   # open alerts for the supervisor
        "sites": sites,
        "workers": results,
    }
    write_json_safely(SCORES_FILE, output)


class AlertManager:
    """
    Decides when to alert the supervisor.

    An alert OPENS when a worker turns RED, or the AI predicts they'll reach RED.
    If a "predicted" alert's worker then actually turns RED, it ESCALATES.
    It CLEARS once the worker has been calm for CLEAR_AFTER_SIM_SECONDS.
    Each of these moments is written to data/alerts.jsonl.
    """

    def __init__(self):
        self.active = {}      # worker_id -> open heat alert
        self.calm_since = {}  # worker_id -> when they stopped being at risk
        self.sos = {}         # worker_id -> open SOS (help request from the helmet button)

    def update_sos(self, r, now):
        """Open an SOS alert when a helmet starts signalling, close it when it stops."""
        wid = r["worker_id"]
        pressed = r.get("sos_pressed_at")
        alert = self.sos.get(wid)
        if pressed and (alert is None or alert["pressed_at"] != pressed):
            alert = {
                "alert_id": f"{wid}-sos-{int(now * 1000)}",
                "worker_id": wid,
                "site": r["site"],
                "job": r["job"],
                "zone": r.get("zone"),
                "kind": "sos",
                "pressed_at": pressed,          # simulated time of the button press
                "started_at": timestamp(),
                "started_sim": self.sim_time,
            }
            self.sos[wid] = alert
            self._log("sos", alert, f"{wid} held the helmet SOS button: help requested")
        elif not pressed and alert is not None:
            self._log("sos_cleared", alert, f"{wid}'s helmet stopped signalling SOS")
            del self.sos[wid]
            alert = None
        if alert is not None:
            # Latest vitals, for the help-request banner.
            alert["heart_rate_bpm"] = r["avg_heart_rate_bpm"]
            alert["skin_temp_c"] = r["avg_skin_temp_c"]
            alert["motionless_s"] = r.get("motionless_s") or 0
            alert["current_level"] = r["level"]
        r["sos_alert"] = alert

    def update(self, results, speed, sim_time=None):
        now = time.time()
        self.sim_time = sim_time
        for r in results:
            wid = r["worker_id"]
            self.update_sos(r, now)
            forecast = r.get("forecast")
            if r["level"] == "RED":
                kind = "red"
            elif forecast and forecast["level"] == "RED":
                kind = "predicted_red"
            else:
                kind = None

            alert = self.active.get(wid)
            if kind:
                self.calm_since.pop(wid, None)
                if alert is None:
                    alert = {
                        "alert_id": f"{wid}-{int(now * 1000)}",
                        "worker_id": wid,
                        "site": r["site"],
                        "job": r["job"],
                        "zone": r.get("zone"),
                        "kind": kind,
                        "started_at": timestamp(),
                        "started_sim": sim_time,
                    }
                    self.active[wid] = alert
                    self._log("alert", alert, self._message(r, kind))
                elif kind == "red" and alert["kind"] == "predicted_red":
                    alert["kind"] = "red"
                    alert["escalated_at"] = timestamp()
                    self._log("alert_escalated", alert, self._message(r, kind))
                # Keep the latest details so the pop-up stays up to date, but never
                # replace a RED alert's message with a "predicted" one (that would
                # read "W13 is RED ... predicted to reach RED").
                if kind == alert["kind"]:
                    alert["message"] = self._message(r, kind)
                alert["minutes"] = forecast["minutes"] if forecast and kind == "predicted_red" else None
                alert["probability"] = forecast["probability"] if forecast else None
            elif alert is not None:
                since = self.calm_since.setdefault(wid, now)
                if (now - since) * speed >= CLEAR_AFTER_SIM_SECONDS:
                    self._log("alert_cleared", alert, f"{wid} is back to {r['level']}")
                    del self.active[wid]
                    del self.calm_since[wid]

            if wid in self.active:
                # The worker's level right now (an open RED alert may be YELLOW again).
                self.active[wid]["current_level"] = r["level"]
            r["alert"] = self.active.get(wid)
        # SOS help requests first: they're the most urgent.
        return list(self.sos.values()) + list(self.active.values())

    @staticmethod
    def _message(r, kind):
        if kind == "red":
            return f"{r['worker_id']} is RED: " + ("; ".join(r["reasons"]) or "high heat stress")
        f = r["forecast"]
        return (f"{r['worker_id']} predicted to reach RED in ~{f['minutes']} min "
                f"({f['probability']:.0%} likely)")

    def _log(self, event_type, alert, message):
        entry = {
            "time": timestamp(),
            "sim_time": getattr(self, "sim_time", None),  # simulated clock, for display
            "type": event_type,
            "alert_id": alert["alert_id"],
            "worker_id": alert["worker_id"],
            "kind": alert["kind"],
            "message": message,
        }
        print(f"  ALERT LOG: {event_type}: {message}")
        ALERTS_FILE.parent.mkdir(exist_ok=True)
        with ALERTS_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")


def timestamp():
    return datetime.now().isoformat(timespec="seconds")


def write_json_safely(path, data):
    """
    Write to a temporary file first, then swap it in. This way the dashboard
    never reads a half-written file.
    """
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    for _ in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:  # Windows: the file is being read right now
            time.sleep(0.05)


def read_settings(previous):
    """
    Return (offline?, version) from the dashboard switch's settings file.
    The version goes up by one every time someone flips the switch.
    If the file is missing, or can't be read this instant (e.g. it's being
    replaced), keep the previous setting rather than guessing.
    """
    try:
        settings = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        return settings.get("weather_mode") == "offline", int(settings.get("version", 0))
    except (OSError, ValueError):
        return previous


def main():
    parser = argparse.ArgumentParser(description="Score heat-stress risk for each worker.")
    parser.add_argument("--watch", action="store_true",
                        help="keep re-scoring every few seconds")
    parser.add_argument("--interval", type=float, default=5,
                        help="seconds between updates in --watch mode (default: 5)")
    parser.add_argument("--offline", action="store_true",
                        help="don't call the weather API; use the simulator's temperatures")
    parser.add_argument("--speed", type=float, default=1,
                        help="match the simulator's --speed so scores update as fast as readings arrive")
    args = parser.parse_args()
    # At high speed, re-score more often (but at most once a second).
    loop_seconds = max(1.0, args.interval / args.speed) if args.speed > 1 else args.interval

    os.system("")  # turns on colour support in older Windows terminals
    weather_cache = WeatherCache(offline=args.offline)
    predictor = load_predictor()  # None if no trained model yet

    if not args.watch:
        run_once(weather_cache, predictor, speed=args.speed)
        return

    print("Watching for new readings. Press Ctrl+C to stop.")
    settings = (args.offline, 0)
    alert_manager = AlertManager()
    site_history = SiteHistory()
    agent = Agent()
    try:
        while True:
            # The dashboard switch (if it has been used) overrides --offline.
            settings = read_settings(previous=settings)
            offline, version = settings
            weather_cache.set_offline(offline)
            run_once(weather_cache, predictor, speed=args.speed, settings_version=version,
                     alert_manager=alert_manager, site_history=site_history, agent=agent)
            time.sleep(loop_seconds)
    except KeyboardInterrupt:
        print("\nStopped by user.")


if __name__ == "__main__":
    main()
