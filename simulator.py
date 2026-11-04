"""
Heat-stress wearable simulator.

Pretends to be 15 outdoor workers in the UAE wearing smart wristbands.
Every few seconds, each "wristband" sends a reading with:
  - heart rate (beats per minute)
  - skin temperature (degrees Celsius)
  - activity level (resting / light / moderate / heavy)

Readings are printed to the screen and saved to data/readings.jsonl
(one JSON reading per line) so other parts of the project can use them.

Run it with:   python simulator.py
Stop it with:  Ctrl+C
"""

import argparse
import json
import random
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Settings you can tweak
# ---------------------------------------------------------------------------

NUM_WORKERS = 15
DEFAULT_INTERVAL_SECONDS = 5
OUTPUT_FILE = Path("data") / "readings.jsonl"

# Typical outdoor jobs and sites in the UAE.
JOBS = ["construction", "road works", "landscaping", "delivery rider", "oil & gas site"]
SITES = {
    "Dubai": 42.0,      # starting outdoor air temperature in Celsius (summer)
    "Abu Dhabi": 43.0,
    "Sharjah": 41.0,
    "Al Ain": 45.0,
}

# How much each activity level raises heart rate above resting.
ACTIVITY_HEART_BOOST = {"resting": 0, "light": 20, "moderate": 45, "heavy": 70}

# How much each activity level adds to body heat each reading.
ACTIVITY_HEAT_GAIN = {"resting": -0.4, "light": -0.05, "moderate": 0.1, "heavy": 0.2}

# Chance of switching to each activity next, given the current activity.
# This keeps the activity realistic (people don't jump from resting to heavy
# and back every 5 seconds).
ACTIVITY_CHANGES = {
    "resting":  {"resting": 0.80, "light": 0.15, "moderate": 0.05, "heavy": 0.00},
    "light":    {"resting": 0.10, "light": 0.65, "moderate": 0.20, "heavy": 0.05},
    "moderate": {"resting": 0.05, "light": 0.25, "moderate": 0.55, "heavy": 0.15},
    "heavy":    {"resting": 0.10, "light": 0.10, "moderate": 0.30, "heavy": 0.50},
}


# ---------------------------------------------------------------------------
# A single simulated worker
# ---------------------------------------------------------------------------

class Worker:
    """One worker with their own body traits and current state."""

    def __init__(self, worker_id, site, job, heat_tolerance):
        self.worker_id = worker_id
        self.site = site
        self.job = job

        # Personal traits: everyone is a bit different.
        self.resting_heart_rate = random.randint(60, 80)
        # 1.0 = average. Lower = struggles more in the heat.
        self.heat_tolerance = heat_tolerance

        # Current state. These change over time.
        self.activity = random.choice(["resting", "light", "moderate"])
        self.heart_rate = float(self.resting_heart_rate)
        self.skin_temp = 34.0       # normal skin temperature in shade
        self.heat_strain = 0.0      # hidden "build-up" of heat in the body (0 = fine)

    def update(self, air_temp):
        """Move the worker forward by one time step (one reading)."""
        self._pick_next_activity()

        # 1) Heat strain builds up when working hard in hot air and
        #    goes down when resting. Hotter air = faster build-up.
        heat_factor = max(0.0, (air_temp - 35) / 10)  # 0 at 35C, 1 at 45C
        #    Workers with low heat tolerance heat up faster AND cool down slower.
        gain = ACTIVITY_HEAT_GAIN[self.activity]
        if gain > 0:
            gain = gain * (0.5 + heat_factor) / self.heat_tolerance
        else:
            gain = gain * self.heat_tolerance
        self.heat_strain = min(10.0, max(0.0, self.heat_strain + gain))

        # 2) Heart rate: resting + activity + extra from heat strain.
        target_hr = (
            self.resting_heart_rate
            + ACTIVITY_HEART_BOOST[self.activity]
            + self.heat_strain * 4
        )
        # Move part of the way towards the target (bodies don't change instantly),
        # then add a little random "sensor noise".
        self.heart_rate += (target_hr - self.heart_rate) * 0.3
        self.heart_rate += random.uniform(-2, 2)

        # 3) Skin temperature: rises with air temperature and heat strain.
        target_skin = 33.5 + heat_factor * 1.0 + self.heat_strain * 0.3
        self.skin_temp += (target_skin - self.skin_temp) * 0.2
        self.skin_temp += random.uniform(-0.05, 0.05)

    def _pick_next_activity(self):
        options = ACTIVITY_CHANGES[self.activity]
        self.activity = random.choices(
            population=list(options.keys()),
            weights=list(options.values()),
        )[0]

    def reading(self):
        """Package the current state as a reading, like a wristband would send."""
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "worker_id": self.worker_id,
            "site": self.site,
            "job": self.job,
            "heart_rate_bpm": round(self.heart_rate),
            "skin_temp_c": round(self.skin_temp, 1),
            "activity_level": self.activity,
        }


# ---------------------------------------------------------------------------
# Setting up the crew and the weather
# ---------------------------------------------------------------------------

def create_workers():
    workers = []
    site_names = list(SITES.keys())
    for i in range(NUM_WORKERS):
        worker_id = f"W{i + 1:02d}"           # W01, W02, ... W15
        site = site_names[i % len(site_names)]
        job = JOBS[i % len(JOBS)]
        # Make 3 workers less heat tolerant (e.g. new to the job, not
        # acclimatised) so the demo shows some people getting into trouble.
        tolerance = 0.5 if i in (2, 7, 11) else random.uniform(0.9, 1.3)
        workers.append(Worker(worker_id, site, job, tolerance))
    return workers


def update_air_temps(air_temps):
    """Let each site's air temperature drift slightly, staying in a UAE summer range."""
    for site in air_temps:
        air_temps[site] += random.uniform(-0.2, 0.2)
        air_temps[site] = min(49.0, max(36.0, air_temps[site]))


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def print_round(round_number, readings, air_temps):
    print(f"\n=== Round {round_number}  ({datetime.now():%H:%M:%S}) ===")
    temps = "  ".join(f"{site}: {t:.1f}C" for site, t in air_temps.items())
    print(f"Air temperature -> {temps}")
    print(f"{'ID':<4} {'Site':<10} {'Job':<15} {'HR':>4} {'Skin':>6}  Activity")
    for r in readings:
        print(
            f"{r['worker_id']:<4} {r['site']:<10} {r['job']:<15} "
            f"{r['heart_rate_bpm']:>4} {r['skin_temp_c']:>5}C  {r['activity_level']}"
        )


def main():
    parser = argparse.ArgumentParser(description="Simulate heat-stress wearable data.")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SECONDS,
                        help="seconds between readings (default: 5)")
    parser.add_argument("--rounds", type=int, default=0,
                        help="stop after this many rounds (default: 0 = run forever)")
    parser.add_argument("--seed", type=int, default=None,
                        help="random seed, so you get the same results every run")
    parser.add_argument("--speed", type=float, default=1,
                        help="make time run faster for demos, e.g. 15 = 15x faster. Each "
                             "reading still counts as 5 s of 'worker time', they just arrive sooner")
    args = parser.parse_args()
    real_interval = args.interval / args.speed

    if args.seed is not None:
        random.seed(args.seed)

    workers = create_workers()
    air_temps = dict(SITES)
    OUTPUT_FILE.parent.mkdir(exist_ok=True)

    print(f"Simulating {len(workers)} workers, one reading every {args.interval}s"
          + (f" (at {args.speed:g}x speed: really every {real_interval:.2f}s)." if args.speed != 1 else "."))
    print(f"Saving readings to {OUTPUT_FILE}. Press Ctrl+C to stop.")

    round_number = 0
    try:
        while args.rounds == 0 or round_number < args.rounds:
            round_number += 1
            update_air_temps(air_temps)

            readings = []
            for worker in workers:
                worker.update(air_temps[worker.site])
                reading = worker.reading()
                reading["air_temp_c"] = round(air_temps[worker.site], 1)
                readings.append(reading)

            # "Send" the readings: append them to the file, one per line.
            with OUTPUT_FILE.open("a", encoding="utf-8") as f:
                for reading in readings:
                    f.write(json.dumps(reading) + "\n")

            print_round(round_number, readings, air_temps)
            time.sleep(real_interval)
    except KeyboardInterrupt:
        print("\nStopped by user.")


if __name__ == "__main__":
    main()
