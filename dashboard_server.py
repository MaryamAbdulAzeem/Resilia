"""
Tiny web server for the heat-stress dashboard (uses only Python's built-in tools).

It does three things:
  GET  /               -> sends the dashboard web page (dashboard/index.html)
  GET  /api/scores     -> sends the latest data/risk_scores.json
  GET  /api/mode       -> tells the page whether we're on live or offline weather
  POST /api/mode       -> the page's switch calls this to change the weather mode
  POST /api/actions    -> a supervisor clicked "Dispatch water", "Schedule shade break"
                          or "Acknowledge"; saved to data/actions.jsonl
  GET  /api/log        -> alerts (from the risk engine) + supervisor actions, newest first

Run it with:  python dashboard_server.py
Then open:    http://localhost:8000
"""

import argparse
import json
import os
import re
import threading
import time
from datetime import datetime
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DASHBOARD_DIR = BASE_DIR / "dashboard"
SCORES_FILE = BASE_DIR / "data" / "risk_scores.json"
SETTINGS_FILE = BASE_DIR / "data" / "settings.json"
ALERTS_FILE = BASE_DIR / "data" / "alerts.jsonl"    # written by the risk engine
ACTIONS_FILE = BASE_DIR / "data" / "actions.jsonl"  # written here, when supervisors click

# The only actions a supervisor can log, and how they read in the log.
ACTIONS = {
    "dispatch_water": "Water dispatched",
    "shade_break": "Shade break scheduled",
    "acknowledge": "Alert acknowledged",
}
WORKER_ID = re.compile(r"^W\d{2}$")
ALERT_ID = re.compile(r"^W\d{2}-\d{1,16}$")
LOG_LIMIT = 200  # newest entries sent to the dashboard

settings_lock = threading.Lock()  # two clicks at once can't both write the file
actions_lock = threading.Lock()


def read_jsonl(path):
    """Read a one-JSON-per-line file, skipping any half-written line."""
    entries = []
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    entries.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return entries


def current_worker_status(worker_id):
    """What the worker looked like when the supervisor acted (for the log)."""
    try:
        scores = json.loads(SCORES_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    for w in scores.get("workers", []):
        if w["worker_id"] == worker_id:
            return {"level": w["level"], "forecast": w.get("forecast")}
    return {}


def log_action(worker_id, action, alert_id):
    status = current_worker_status(worker_id)
    entry = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "type": "action",
        "action": action,
        "label": ACTIONS[action],
        "worker_id": worker_id,
        "alert_id": alert_id,
        "worker_level": status.get("level"),
    }
    with actions_lock:
        ACTIONS_FILE.parent.mkdir(exist_ok=True)
        with ACTIONS_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    print(f"Supervisor action: {entry['label']} for {worker_id}")
    return entry


def read_settings():
    try:
        settings = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        return {"weather_mode": settings.get("weather_mode", "live"),
                "version": int(settings.get("version", 0))}
    except (OSError, ValueError):
        return {"weather_mode": "live", "version": 0}


def write_mode(mode):
    """
    Save the switch position with a version number that goes up on every click.
    The risk engine reports which version it has applied, so the dashboard
    knows exactly when the change has really happened.
    """
    with settings_lock:
        settings = {"weather_mode": mode, "version": read_settings()["version"] + 1}
        SETTINGS_FILE.parent.mkdir(exist_ok=True)
        tmp = SETTINGS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(settings), encoding="utf-8")
        for attempt in range(10):
            try:
                os.replace(tmp, SETTINGS_FILE)
                break
            except PermissionError:  # Windows: the engine is reading the file right now
                time.sleep(0.05)
        return settings


class DashboardHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/api/scores"):
            try:
                body = SCORES_FILE.read_bytes()
            except OSError:
                return self.send_json({"error": "No scores yet. Is the risk engine running?"}, 503)
            return self.send_raw_json(body)
        if self.path.startswith("/api/mode"):
            return self.send_json(read_settings())
        if self.path.startswith("/api/log"):
            entries = read_jsonl(ALERTS_FILE) + read_jsonl(ACTIONS_FILE)
            entries.sort(key=lambda e: e.get("time", ""), reverse=True)
            return self.send_json({"entries": entries[:LOG_LIMIT]})
        # Anything else: serve files from the dashboard folder (index.html etc.)
        return super().do_GET()

    def read_body(self):
        """Read the JSON the page sent. Returns {} if it's missing or not valid."""
        try:
            length = min(int(self.headers.get("Content-Length", 0)), 10_000)
            body = json.loads(self.rfile.read(length))
            return body if isinstance(body, dict) else {}
        except ValueError:
            return {}

    def do_POST(self):
        if self.path.startswith("/api/actions"):
            return self.handle_action()
        if not self.path.startswith("/api/mode"):
            return self.send_json({"error": "not found"}, 404)
        mode = self.read_body().get("weather_mode")
        if mode not in ("live", "offline"):
            return self.send_json({"error": "weather_mode must be 'live' or 'offline'"}, 400)
        settings = write_mode(mode)
        print(f"Dashboard switched weather mode to: {mode} (version {settings['version']})")
        return self.send_json(settings)

    def handle_action(self):
        body = self.read_body()
        worker_id, action, alert_id = body.get("worker_id"), body.get("action"), body.get("alert_id")
        # Only accept exactly what the dashboard's buttons send.
        if not isinstance(worker_id, str) or not WORKER_ID.match(worker_id):
            return self.send_json({"error": "worker_id must look like W08"}, 400)
        if action not in ACTIONS:
            return self.send_json({"error": f"action must be one of {list(ACTIONS)}"}, 400)
        if alert_id is not None and (not isinstance(alert_id, str) or not ALERT_ID.match(alert_id)):
            return self.send_json({"error": "bad alert_id"}, 400)
        return self.send_json(log_action(worker_id, action, alert_id))

    def send_json(self, data, status=200):
        self.send_raw_json(json.dumps(data).encode("utf-8"), status)

    def send_raw_json(self, body, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")  # always fetch fresh data
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # keep the terminal quiet (the page asks for data every few seconds)


def main():
    parser = argparse.ArgumentParser(description="Serve the heat-stress dashboard.")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    handler = partial(DashboardHandler, directory=str(DASHBOARD_DIR))
    # 127.0.0.1 = only this computer can open it (safer on hackathon wifi).
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    print(f"Dashboard running at http://localhost:{args.port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
