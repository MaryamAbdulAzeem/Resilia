"""
Start the whole prototype with one command:

    python run_all.py                   normal speed
    python run_all.py --demo            demo: 15x speed, summer scenario, W08 & W12
                                        turn RED in about 40 seconds (same every time)
    python run_all.py --demo --share    ...and a temporary public VIEW-ONLY link
                                        (Cloudflare quick tunnel) for teammates' phones
    python run_all.py --demo --loop 20  always-on demo: the day restarts every 20 min
                                        (used on Render, see render.yaml)

This launches these programs side by side:
  1. simulator.py         - fake wristbands sending readings every 5 s
  2. risk_engine.py       - scores every worker every 5 s
  3. dashboard_server.py  - the web page at http://localhost:8000
  4. cloudflared          - only with --share: the public link

Their output goes to data/logs/ so this window stays readable.
Press Ctrl+C here to stop all of them.
"""

import argparse
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from dashboard_server import SHARE_URL_FILE, is_hosted, write_mode

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = DATA_DIR / "logs"

DEMO_SPEED = 15
# Seed 15 was chosen by testing: with it, W08 and W12 turn RED after about
# 10 simulated minutes (~40 real seconds at 15x), so the AI forecast appears first.
DEMO_SEED = 15
# With this start time and seed, the first SOS (W02) comes at ~28 s; the AI agent
# steps in from the first minute, and W12 / W08 usually still reach the danger
# zone at ~75-95 s (checked by replaying the simulation with the agent).
DEMO_START = "11:30"

# Where winget installs cloudflared, in case it isn't on PATH yet.
CLOUDFLARED_PATHS = [
    r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
    r"C:\Program Files\cloudflared\cloudflared.exe",
]
TUNNEL_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
TUNNEL_RESTARTS = 3  # if the public link drops, try a fresh one up to this many times


def find_cloudflared():
    found = shutil.which("cloudflared")
    if found:
        return found
    return next((p for p in CLOUDFLARED_PATHS if Path(p).exists()), None)


def history_needed():
    """True if the simulated 30-day history (for Insights) is missing or out of date."""
    from generate_history import OUT_FILE, VERSION
    try:
        return json.loads((BASE_DIR / OUT_FILE).read_text(encoding="utf-8")).get("version") != VERSION
    except (OSError, ValueError):
        return True


STREAM_OUTPUT = False       # also copy programs' output into this window (set when hosted / --stream)
TABLE_EVERY_SECONDS = 60    # routine tables ("=== Round", "=== Heat-stress report"): one a minute


def _pump(name, proc, log_file):
    """Copy one program's output to its log file and (when streaming) to our own output."""
    in_table = False
    last_table = float("-inf")
    show_table = False
    try:
        for line in proc.stdout:
            log_file.write(line)
            log_file.flush()
            if not STREAM_OUTPUT:
                continue
            text = line.rstrip("\n")
            # A routine table runs from its "=== ..." header to the next blank line.
            if text.startswith("=== "):
                in_table = True
                show_table = time.time() - last_table >= TABLE_EVERY_SECONDS
                if show_table:
                    last_table = time.time()
            elif in_table and not text.strip():
                in_table = False
                continue
            if in_table and not show_table:
                continue   # skip most routine tables; errors and events always come through
            if text.strip():
                print(f"[{name}] {text}", flush=True)
    except ValueError:
        pass   # the log file was closed while stopping
    finally:
        try:
            log_file.close()
        except OSError:
            pass


def start_program(name, command, log_name):
    """Start one program; its output goes to data/logs/<log_name> (and, when hosted, to our output)."""
    log_file = open(LOG_DIR / log_name, "w", encoding="utf-8")
    proc = subprocess.Popen(command, cwd=BASE_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", bufsize=1)
    pump = threading.Thread(target=_pump, args=(name, proc, log_file), daemon=True)
    pump.start()
    return {"name": name, "proc": proc, "log": log_file, "log_name": log_name, "pump": pump}


def start_tunnel(cloudflared):
    """Start a Cloudflare quick tunnel and wait (up to 40 s) for its public link."""
    tunnel = start_program("public link", [cloudflared, "tunnel", "--no-autoupdate",
                                           "--url", "http://localhost:8000"], "cloudflared.log")
    deadline = time.time() + 40
    while time.time() < deadline and tunnel["proc"].poll() is None:
        time.sleep(1)
        match = TUNNEL_URL.search((LOG_DIR / "cloudflared.log").read_text(encoding="utf-8", errors="ignore"))
        if match:
            tunnel["url"] = match.group(0)
            SHARE_URL_FILE.write_text(tunnel["url"], encoding="utf-8")  # the laptop's dashboard shows it
            return tunnel
    tunnel["url"] = None
    return tunnel


def announce_link(url):
    line = "=" * 64
    print(f"\n{line}\n  PUBLIC VIEW-ONLY LINK (share with your team):\n\n    {url}\n")
    print("  Anyone with this link can WATCH the dashboard but not press")
    print("  buttons or change the weather. Keep this laptop awake and online.")
    print(f"{line}\n")


def stop(program):
    if program["proc"].poll() is None:
        program["proc"].terminate()
    try:
        program["proc"].wait(timeout=10)
    except subprocess.TimeoutExpired:
        program["proc"].kill()
    program["pump"].join(timeout=5)   # the pump closes the log file once the output ends


def main():
    parser = argparse.ArgumentParser(description="Start the whole heat-stress prototype.")
    parser.add_argument("--demo", action="store_true",
                        help=f"{DEMO_SPEED}x speed, summer scenario, repeatable results")
    parser.add_argument("--speed", type=float, default=None,
                        help="custom speed (e.g. 5); --demo uses 15")
    parser.add_argument("--share", action="store_true",
                        help="also create a temporary public view-only link (Cloudflare quick tunnel)")
    parser.add_argument("--loop", type=float, default=None, metavar="MINUTES",
                        help="restart the demo day every MINUTES real minutes (for an always-on "
                             "public demo, so visitors always see the midday heat, never night)")
    parser.add_argument("--stream", action="store_true",
                        help="also show the programs' output here (always on when hosted, e.g. on Render)")
    args = parser.parse_args()
    global STREAM_OUTPUT
    STREAM_OUTPUT = args.stream or is_hosted()

    cloudflared = None
    if args.share:
        cloudflared = find_cloudflared()
        if not cloudflared:
            print("--share needs Cloudflare's 'cloudflared' tool, which isn't installed.")
            print("Install it with:  winget install --id Cloudflare.cloudflared -e")
            sys.exit(1)

    speed = args.speed or (DEMO_SPEED if args.demo else 1)
    simulator_args = ["simulator.py", "--speed", str(speed)]
    if args.demo:
        # Start the simulated clock late morning, as the day heats up.
        simulator_args += ["--seed", str(DEMO_SEED), "--start", DEMO_START]

    programs = [
        ("simulator", simulator_args),
        ("risk engine", ["risk_engine.py", "--watch", "--speed", str(speed)]),
        ("dashboard", ["dashboard_server.py"]),
    ]

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    SHARE_URL_FILE.unlink(missing_ok=True)  # an old link is dead; don't show it
    hosted = is_hosted()   # RESILIA_PUBLIC or Render's PORT: same rule as the dashboard server

    # A host (e.g. Render) stops the service with SIGTERM: stop the programs cleanly too.
    def on_terminate(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, on_terminate)

    def fresh_start():
        """Start from a clean slate so old test readings don't mix in."""
        (DATA_DIR / "readings.jsonl").unlink(missing_ok=True)
        # Breaks / water / pauses use the simulated clock, which restarts each run.
        for stale in ("controls.json", "agent_state.json", "insights.json", "day_history.json", "risk_scores.json"):
            (DATA_DIR / stale).unlink(missing_ok=True)
        if args.demo:
            # W08 and W12 only turn RED in summer heat, so start the demo there.
            # (You can still flip the switch on the dashboard.)
            write_mode("offline")
            # Each demo starts with an empty alert/action log. (Normal runs keep it.)
            for log in ("alerts.jsonl", "actions.jsonl", "agent.jsonl"):
                (DATA_DIR / log).unlink(missing_ok=True)

    def start_all():
        fresh_start()
        started = []
        for name, command in programs:
            log_name = command[0].replace(".py", ".log")
            # -u: write logs straight away, no buffering
            started.append(start_program(name, [sys.executable, "-u", *command], log_name))
            print(f"Started {name} (logs: data/logs/{log_name})", flush=True)
            time.sleep(1)  # give each one a moment, so the engine has readings to score
        return started

    if args.demo:
        print(f"DEMO MODE: {speed:g}x speed, summer scenario, seed {DEMO_SEED}.")
    elif speed != 1:
        print(f"Running at {speed:g}x speed.")
    if args.loop:
        print(f"The demo day restarts every {args.loop:g} minutes.")

    running = []
    tunnel = None
    history = None   # background history builder (optional)
    try:
        running = start_all()
        cycle_started = time.time()

        if hosted:
            print(f"\nHosted online: dashboard listening on 0.0.0.0:{os.environ.get('PORT') or 10000}, "
                  "every visitor is view-only.", flush=True)
        else:
            print("\nOpen http://localhost:8000 in your browser (full control on this laptop).")
        print("The first scores appear after about 5 seconds. Press Ctrl+C to stop everything.")

        # Insights' "Past 30 days" view uses a simulated history (models/history_30d.json,
        # included in the project). Rebuild it in the background only if it's missing
        # or out of date; the demo doesn't wait for it.
        # (Never on a small hosted server: it uses several cores for minutes.)
        if history_needed() and not hosted:
            history = start_program("30-day history", [sys.executable, "-u", "generate_history.py"], "history.log")
            print("\nBuilding the simulated 30-day history for Insights in the background "
                  "(about 3 minutes, one time only). Logs: data/logs/history.log")

        restarts_left = TUNNEL_RESTARTS
        if args.share:
            print("\nCreating the public link (takes a few seconds)...")
            tunnel = start_tunnel(cloudflared)
            if tunnel["url"]:
                announce_link(tunnel["url"])
            else:
                print("! Couldn't get a public link. See data/logs/cloudflared.log. "
                      "The dashboard still works on this laptop.")

        while True:
            for program in running:
                if program["proc"].poll() is not None:
                    print(f"! The {program['name']} stopped unexpectedly. Check data/logs/ for details.")
                    program["pump"].join(timeout=5)
                    # Show why (the end of its log), then stop everything.
                    print(f"--- last lines of data/logs/{program['log_name']} (exit code {program['proc'].returncode}):")
                    print((LOG_DIR / program["log_name"]).read_text(encoding="utf-8", errors="ignore")[-3000:], flush=True)
                    raise KeyboardInterrupt
            # Always-on demo: start the day again before the sun goes down.
            if args.loop and time.time() - cycle_started > args.loop * 60:
                print("\nRestarting the demo day...", flush=True)
                for program in running:
                    stop(program)
                running = start_all()
                cycle_started = time.time()
            # The public link is optional: if it drops, keep the demo running
            # and try a fresh link (it will have a new address).
            if tunnel and tunnel["proc"].poll() is not None:
                stop(tunnel)
                SHARE_URL_FILE.unlink(missing_ok=True)
                if restarts_left > 0:
                    restarts_left -= 1
                    print("! The public link dropped. Creating a new one...")
                    tunnel = start_tunnel(cloudflared)
                    if tunnel["url"]:
                        announce_link(tunnel["url"])
                else:
                    print("! The public link dropped again. The dashboard still works on this laptop.")
                    tunnel = None
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        for program in running + ([tunnel] if tunnel else []) + ([history] if history else []):
            stop(program)
        SHARE_URL_FILE.unlink(missing_ok=True)
        print("All stopped.")


if __name__ == "__main__":
    main()
