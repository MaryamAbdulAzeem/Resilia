"""
Generate 30 days of SIMULATED history for the Insights page.

Replays 30 summer working days (06:00-18:00) with the same simulator, risk
engine, AI forecast and AI agent used live, as fast as the computer can go,
and saves the combined insights to data/history_30d.json.

  - Each day has its own random seed and slightly hotter or cooler weather.
  - The agent really acts (breaks, water, crew pauses), so the numbers
    include its effect, just like the live system.
  - Same seeds -> same history every time.

This is simulated data, not field data. The Insights page labels it so.

Run it with:  python generate_history.py      (takes a few minutes, once)
run_all.py starts it automatically if the file is missing or out of date.
"""

import builtins
import json
import multiprocessing
import os
import random
import sys
import tempfile
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

import agent as agent_mod
import risk_engine as engine
import simulator as sim
from insights import ShiftStats
from predictor import HISTORY_READINGS, load_predictor

VERSION = 2                    # bump when the history logic changes (run_all regenerates)
DAYS = 30
SHIFT_START, SHIFT_END = 6, 18
STEP_SECONDS = 5
SCORE_EVERY_STEPS = 12         # score once per simulated minute
# Saved next to the AI model (and committed to Git), so Insights is complete
# straight away on any computer. Regenerated only when VERSION changes.
OUT_FILE = Path("models") / "history_30d.json"
PROGRESS_FILE = Path("data") / "history_progress.json"


def write_json(path, data):
    path.parent.mkdir(exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    for _ in range(10):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05)


def score_cycle(history, weather, predictor, sim_iso):
    """The risk engine's per-worker scoring (same as live), for one simulated minute."""
    results, hists = [], []
    for wid, hist in history.items():
        h = list(hist)
        latest = h[-1]
        wx = weather[latest["site"]]
        r = engine.score_worker(h[-engine.WINDOW:], wx)
        for key in ("helmet_id", "gateway", "zone", "battery_pct", "sos_pressed_at", "motionless_s", "on_break"):
            r[key] = latest.get(key)
        r["sim_time"] = sim_iso
        r["hr_change_5m"] = engine.trend_5m(h, "heart_rate_bpm")
        r["skin_change_5m"] = engine.trend_5m(h, "skin_temp_c")
        results.append(r)
        hists.append(h)
    # AI forecast for every worker, like live, but in ONE model call (much faster).
    forecasts = predictor.forecast_many([(h, r["wbgt_c"], r["level"], r["score"]) for h, r in zip(hists, results)]) \
        if predictor else [None] * len(results)
    for r, f in zip(results, forecasts):
        r["forecast"] = f
        r["status"] = engine.crew_status(r)
    return results


def by_week(by_day, first_day):
    """Group daily alert/intervention counts into weeks W1, W2, ..."""
    weeks = {}
    for d, v in by_day.items():
        idx = (datetime.fromisoformat(d).date() - first_day).days // 7
        w = weeks.setdefault(idx, {"label": f"W{idx + 1}", "alerts": 0, "interventions": 0, "days": 0})
        w["alerts"] += v["alerts"]
        w["interventions"] += v["interventions"]
        w["days"] += 1
    return [weeks[k] for k in sorted(weeks)]


_predictor = None


def _setup_worker():
    """Runs once in each worker process: keep the replay's files out of the live data folder."""
    global _predictor
    tmp = Path(tempfile.mkdtemp(prefix="resilia_history_"))
    engine.ALERTS_FILE = tmp / "alerts.jsonl"
    agent_mod.AGENT_LOG = tmp / "agent.jsonl"
    agent_mod.ACTIONS_FILE = tmp / "no_actions.jsonl"
    agent_mod._write_json = lambda path, data: None   # the agent's state/controls stay in memory
    _predictor = load_predictor()
    if _predictor is not None:
        _predictor.model.n_jobs = 1   # small batches: one core per process is fastest


def run_day(args):
    """Simulate one working day (day 0 = the oldest); return (day, its ShiftStats)."""
    day, days, today = args
    stats = ShiftStats(period="over the past 30 days")
    date = today - timedelta(days=days - day)
    seed = 2000 + day
    random.seed(seed)
    sim.device_rng.seed(seed + 500)
    weather_rng = random.Random(seed + 900)
    day_offset = weather_rng.uniform(-3.0, 2.0)          # a hotter or cooler day
    workers = sim.create_workers()
    base = {s: t + day_offset + weather_rng.uniform(-0.7, 0.7) for s, t in sim.SITES.items()}
    am = engine.AlertManager()
    ag = agent_mod.Agent()
    am.listener = stats.on_event
    ag.listener = stats.on_event
    history = {w.worker_id: deque(maxlen=HISTORY_READINGS) for w in workers}

    quiet = builtins.print
    builtins.print = lambda *a, **k: None   # silence per-step logging
    try:
        t = date.replace(hour=SHIFT_START)
        end = date.replace(hour=SHIFT_END)
        step = 0
        air_now = {}
        while True:
            step += 1
            t += timedelta(seconds=STEP_SECONDS)
            if t >= end:   # the shift ends at 18:00 (don't count a lone 18:00 minute)
                break
            hour = sim.hour_of(t)
            sim.update_air_temps(base)
            ctl = ag.controls   # the agent's breaks, water runs and crew pauses
            for w in workers:
                air = sim.air_temp_at(base[w.site], hour)
                air_now[w.site] = air
                on_break = sim.active_until(ctl.get("breaks"), w.worker_id, t)
                w.update(air, on_break=on_break,
                         heavy_paused=sim.active_until(ctl.get("pauses"), w.site, t),
                         hydrated=sim.active_until(ctl.get("water"), w.worker_id, t),
                         sun_w_m2=sim.sun_w_m2(hour))
                w.helmet_step(t, STEP_SECONDS)
                r = w.reading()
                r["timestamp"] = r["sim_time"] = t.isoformat(timespec="seconds")
                r["on_break"] = on_break
                history[w.worker_id].append(r)
            if step % SCORE_EVERY_STEPS:
                continue
            sim_iso = t.isoformat(timespec="seconds")
            weather = {s: engine.simulated_weather(a, hour) for s, a in air_now.items()}
            results = score_cycle(history, weather, _predictor, sim_iso)
            sites = {s: {"wbgt_c": engine.estimate_wbgt(wx["air_temp_c"], wx["humidity_pct"], wx["solar_w_m2"]),
                         "history": []} for s, wx in weather.items()}
            alerts = am.update(results, 1, sim_iso)
            ag.step(results, alerts, sites, sim_iso)
            stats.update(results, sites, sim_iso)
    finally:
        builtins.print = quiet
    return day, stats


def main():
    started = time.time()
    today = datetime.now(sim.GST).replace(hour=0, minute=0, second=0, microsecond=0)
    first_day = (today - timedelta(days=DAYS)).date()
    log = lambda *a: print(*a, flush=True)

    # Days are independent, so simulate several at once (one per processor core,
    # leaving one free; at most 4 to keep memory use modest).
    procs = max(1, min(4, (os.cpu_count() or 2) - 1, DAYS))
    log(f"Simulating {DAYS} days of history on {procs} cores...")
    stats = ShiftStats(period="over the past 30 days")
    done = 0
    write_json(PROGRESS_FILE, {"status": "generating", "day": 0, "days": DAYS})
    with multiprocessing.Pool(procs, initializer=_setup_worker) as pool:
        for day, day_stats in pool.imap_unordered(run_day, [(d, DAYS, today) for d in range(DAYS)]):
            stats.merge(day_stats)
            done += 1
            write_json(PROGRESS_FILE, {"status": "generating", "day": done, "days": DAYS})
            log(f"day {day + 1} done ({done}/{DAYS}) · {time.time() - started:.0f}s so far")

    snap = stats.snapshot()
    snap.update({
        "kind": "simulated_history",
        "version": VERSION,
        "days": DAYS,
        "from": first_day.isoformat(),
        "to": (today - timedelta(days=1)).date().isoformat(),
        "shift_hours": [SHIFT_START, SHIFT_END],
        "by_week": by_week(snap.get("by_day", {}), first_day),
        "note": "Simulated history: 30 summer days replayed with the Resilia simulator, risk engine, "
                "AI forecast and AI agent. Not field data.",
        "generated_in_seconds": round(time.time() - started),
    })
    write_json(OUT_FILE, snap)
    PROGRESS_FILE.unlink(missing_ok=True)
    log(f"Saved {OUT_FILE} in {time.time() - started:.0f}s")


if __name__ == "__main__":
    if "--days" in sys.argv:   # quick test run: fewer days, separate file
        DAYS = int(sys.argv[sys.argv.index("--days") + 1])
        OUT_FILE = Path("data") / "history_test.json"
        PROGRESS_FILE = Path("data") / "history_test_progress.json"
    try:
        main()
    except Exception as error:
        write_json(PROGRESS_FILE, {"status": "failed", "error": str(error)})
        raise
