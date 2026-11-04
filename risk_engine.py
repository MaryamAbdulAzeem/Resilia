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
from datetime import datetime
from pathlib import Path

from predictor import HISTORY_READINGS, load_predictor

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

    def get(self, site, fallback_temp):
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
            # Fallback: use the simulator's air temperature and assume a typical
            # UAE summer afternoon (30% humidity, strong sun).
            weather = {
                "air_temp_c": fallback_temp,
                "humidity_pct": 30,
                "solar_w_m2": 800,
                "observed_at": None,
                "source": "simulated",
            }
            if not self.offline:
                # Live weather failed: try again in 1 minute, not 10.
                fetched_at = now - WEATHER_REFRESH_SECONDS + 60
        self.cache[site] = (fetched_at, weather)
        return weather


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


def run_once(weather_cache, predictor=None, speed=1, settings_version=0, alert_manager=None):
    # Keep 6 minutes of history: the last 1 minute is used for today's score,
    # the full 6 minutes lets the AI forecast see trends.
    readings_by_worker = load_recent_readings(history=HISTORY_READINGS)
    if not readings_by_worker:
        print(f"No readings found in {READINGS_FILE}. Start the simulator first: python simulator.py")
        return

    results = []
    sites = {}
    for worker_id in sorted(readings_by_worker):
        history = readings_by_worker[worker_id]
        latest = history[-1]
        site = latest["site"]
        weather = weather_cache.get(site, latest.get("air_temp_c", 40.0))
        result = score_worker(history[-WINDOW:], weather)
        result["lat"], result["lon"] = SITE_COORDS.get(site, (None, None))

        # AI forecast: will this worker's risk rise in the next 15 minutes?
        result["forecast"] = None
        if predictor is not None:
            wbgt = estimate_wbgt(weather["air_temp_c"], weather["humidity_pct"], weather["solar_w_m2"])
            result["forecast"] = predictor.forecast(history, wbgt, result["level"], result["score"])
        results.append(result)
        sites[site] = {
            "lat": result["lat"],
            "lon": result["lon"],
            "air_temp_c": weather["air_temp_c"],
            "humidity_pct": weather["humidity_pct"],
            "solar_w_m2": weather["solar_w_m2"],
            "wbgt_c": result["wbgt_c"],
            "source": weather["source"],
            "observed_at": weather["observed_at"],
        }

    # Most at-risk workers first; among equal scores, rising forecasts first (soonest first).
    def sort_key(r):
        f = r.get("forecast")
        return (-r["score"], 0 if f else 1, f["minutes"] if f else 0, r["worker_id"])
    results.sort(key=sort_key)
    alerts = alert_manager.update(results, speed) if alert_manager else []
    print_report(results)

    mode = "offline" if weather_cache.offline else "live"
    output = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "weather_mode": mode,
        # Which switch change this result reflects (the dashboard waits for it).
        "settings_version": settings_version,
        # In live mode, sites where Open-Meteo failed and simulated values are used.
        "live_weather_failed": sorted(s for s, v in sites.items()
                                      if mode == "live" and v["source"] != "open-meteo"),
        "speed": speed,
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
        self.active = {}      # worker_id -> open alert
        self.calm_since = {}  # worker_id -> when they stopped being at risk

    def update(self, results, speed):
        now = time.time()
        for r in results:
            wid = r["worker_id"]
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
                        "kind": kind,
                        "started_at": timestamp(),
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
        return list(self.active.values())

    @staticmethod
    def _message(r, kind):
        if kind == "red":
            return f"{r['worker_id']} is RED: " + ("; ".join(r["reasons"]) or "high heat stress")
        f = r["forecast"]
        return (f"{r['worker_id']} predicted to reach RED in ~{f['minutes']} min "
                f"({f['probability']:.0%} likely)")

    @staticmethod
    def _log(event_type, alert, message):
        entry = {
            "time": timestamp(),
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
    try:
        while True:
            # The dashboard switch (if it has been used) overrides --offline.
            settings = read_settings(previous=settings)
            offline, version = settings
            weather_cache.set_offline(offline)
            run_once(weather_cache, predictor, speed=args.speed, settings_version=version,
                     alert_manager=alert_manager)
            time.sleep(loop_seconds)
    except KeyboardInterrupt:
        print("\nStopped by user.")


if __name__ == "__main__":
    main()
