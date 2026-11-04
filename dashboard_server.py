"""
Tiny web server for the heat-stress dashboard (uses only Python's built-in tools).

It does three things:
  GET  /               -> sends the dashboard web page (dashboard/index.html)
  GET  /api/scores     -> sends the latest data/risk_scores.json
  GET  /api/mode       -> tells the page whether we're on live or offline weather
  POST /api/mode       -> the page's switch calls this to change the weather mode

Run it with:  python dashboard_server.py
Then open:    http://localhost:8000
"""

import argparse
import json
import os
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DASHBOARD_DIR = BASE_DIR / "dashboard"
SCORES_FILE = BASE_DIR / "data" / "risk_scores.json"
SETTINGS_FILE = BASE_DIR / "data" / "settings.json"


settings_lock = threading.Lock()  # two clicks at once can't both write the file


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
        # Anything else: serve files from the dashboard folder (index.html etc.)
        return super().do_GET()

    def do_POST(self):
        if not self.path.startswith("/api/mode"):
            return self.send_json({"error": "not found"}, 404)
        try:
            length = int(self.headers.get("Content-Length", 0))
            mode = json.loads(self.rfile.read(length)).get("weather_mode")
        except (ValueError, AttributeError):
            mode = None
        if mode not in ("live", "offline"):
            return self.send_json({"error": "weather_mode must be 'live' or 'offline'"}, 400)
        settings = write_mode(mode)
        print(f"Dashboard switched weather mode to: {mode} (version {settings['version']})")
        return self.send_json(settings)

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
