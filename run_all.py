"""
Start the whole prototype with one command:

    python run_all.py           normal speed
    python run_all.py --demo    demo: 15x speed, summer scenario, W08 & W12 turn
                                RED in about 40 seconds (same every time)

This launches three programs side by side:
  1. simulator.py         - fake wristbands sending readings every 5 s
  2. risk_engine.py       - scores every worker every 5 s
  3. dashboard_server.py  - the web page at http://localhost:8000

Their output goes to data/logs/ so this window stays readable.
Press Ctrl+C here to stop all of them.
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

from dashboard_server import write_mode

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = DATA_DIR / "logs"

DEMO_SPEED = 15
# Seed 15 was chosen by testing: with it, W08 and W12 turn RED after about
# 10 simulated minutes (~40 real seconds at 15x), so the AI forecast appears first.
DEMO_SEED = 15


def main():
    parser = argparse.ArgumentParser(description="Start the whole heat-stress prototype.")
    parser.add_argument("--demo", action="store_true",
                        help=f"{DEMO_SPEED}x speed, summer scenario, repeatable results")
    parser.add_argument("--speed", type=float, default=None,
                        help="custom speed (e.g. 5); --demo uses 15")
    args = parser.parse_args()

    speed = args.speed or (DEMO_SPEED if args.demo else 1)
    simulator_args = ["simulator.py", "--speed", str(speed)]
    if args.demo:
        simulator_args += ["--seed", str(DEMO_SEED)]

    programs = [
        ("simulator", simulator_args),
        ("risk engine", ["risk_engine.py", "--watch", "--speed", str(speed)]),
        ("dashboard", ["dashboard_server.py"]),
    ]

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    # Start from a clean slate so old test readings don't mix in.
    (DATA_DIR / "readings.jsonl").unlink(missing_ok=True)
    if args.demo:
        # W08 and W12 only turn RED in summer heat, so start the demo there.
        # (You can still flip the switch on the dashboard.)
        write_mode("offline")
        print(f"DEMO MODE: {speed:g}x speed, summer scenario, seed {DEMO_SEED}.")
    elif speed != 1:
        print(f"Running at {speed:g}x speed.")

    processes = []
    for name, command in programs:
        log_name = command[0].replace(".py", ".log")
        log_file = open(LOG_DIR / log_name, "w", encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, "-u", *command],  # -u: write logs straight away, no buffering
            cwd=BASE_DIR, stdout=log_file, stderr=subprocess.STDOUT,
        )
        processes.append((name, proc, log_file))
        print(f"Started {name} (logs: data/logs/{log_name})")
        time.sleep(1)  # give each one a moment, so the engine has readings to score

    print("\nOpen http://localhost:8000 in your browser.")
    print("The first scores appear after about 5 seconds. Press Ctrl+C to stop everything.\n")

    try:
        while True:
            for name, proc, _ in processes:
                if proc.poll() is not None:
                    print(f"! The {name} stopped unexpectedly. Check data/logs/ for details.")
                    raise KeyboardInterrupt
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        for _, proc, log_file in processes:
            if proc.poll() is None:
                proc.terminate()
            log_file.close()
        print("All stopped.")


if __name__ == "__main__":
    main()
