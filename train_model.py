"""
Train the AI risk forecast.

Steps:
  1. Run the simulator very fast for many practice "shifts" (no waiting).
  2. At each minute of each shift, record the clues (features) for every
     worker AND the highest risk level they actually reached in the next
     1-15 minutes.
  3. Train a random forest on most shifts.
  4. Test it on shifts it has never seen and compare with a simple
     "nothing will change" guess.
  5. Save the model to models/risk_model.pkl and the scores to models/metrics.json.

Run it with:  python train_model.py     (takes a minute or two)
"""

import json
import pickle
import random
import time
from pathlib import Path

from sklearn.ensemble import RandomForestClassifier

import simulator
from predictor import (FEATURE_NAMES, HISTORY_READINGS, HORIZONS, LEVEL_NUM, LEVELS,
                       MODEL_FILE, READINGS_PER_MINUTE, extract_features)
from risk_engine import WINDOW, estimate_wbgt, score_worker

NUM_SHIFTS = 150            # practice shifts (15 workers each)
TEST_SHIFTS = 30            # the last 30 are kept aside for testing
SHIFT_MINUTES = 75          # length of each practice shift
SAMPLE_UNTIL_MINUTE = 60    # stop sampling early so there's always 15 min of "future"
HORIZONS_PER_SAMPLE = 5     # how many future horizons to learn from per sample


def random_live_weather():
    """Weather like Open-Meteo might report: anything from a mild night to a hot day."""
    return {
        "air_temp_c": random.uniform(22, 42),
        "humidity_pct": random.uniform(15, 85),
        "solar_w_m2": random.choice([0, random.uniform(0, 1000)]),
        "source": "training",
    }


def run_shift(shift_number):
    """Simulate one shift. Return training rows AND per-worker minute-by-minute timelines."""
    random.seed(shift_number)
    workers = simulator.create_workers()
    air_temps = dict(simulator.SITES)   # each site's base temperature (drifts slowly)
    # Each practice shift starts at a different time of day, so the model learns
    # how the heat (air temperature + sun) changes from morning to evening.
    start_hour = random.uniform(6.0, 18.0 - SHIFT_MINUTES / 60)

    # Mirror how the real system runs: the risk engine either uses live
    # weather or the offline summer scenario (simulator temperature, 30%, sun for the hour).
    offline = random.random() < 0.5
    live_weather = {site: random_live_weather() for site in air_temps}

    readings = {w.worker_id: [] for w in workers}
    weather_log = {w.worker_id: [] for w in workers}

    for step in range(SHIFT_MINUTES * READINGS_PER_MINUTE):
        hour = start_hour + step * 5 / 3600
        sun = simulator.sun_w_m2(hour)
        simulator.update_air_temps(air_temps)
        for w in workers:
            air = simulator.air_temp_at(air_temps[w.site], hour)
            w.update(air, sun_w_m2=sun)   # same physics as the live simulator
            readings[w.worker_id].append(w.reading())
            if offline:
                weather = {"air_temp_c": air, "humidity_pct": 30, "solar_w_m2": sun, "source": "simulated"}
            else:
                weather = live_weather[w.site]
            weather_log[w.worker_id].append(weather)

    rows = []
    timelines = []  # for the "how early was the warning?" test
    for w in workers:
        r_list, wx_list = readings[w.worker_id], weather_log[w.worker_id]

        # The risk engine's score at the end of every minute.
        scores = {}
        for minute in range(1, SHIFT_MINUTES + 1):
            end = minute * READINGS_PER_MINUTE
            scores[minute] = score_worker(r_list[end - WINDOW:end], wx_list[end - 1])

        timeline = []
        for minute in range(1, SHIFT_MINUTES + 1):
            end = minute * READINGS_PER_MINUTE
            now = scores[minute]
            weather = wx_list[end - 1]
            wbgt = estimate_wbgt(weather["air_temp_c"], weather["humidity_pct"], weather["solar_w_m2"])
            base = extract_features(r_list[max(0, end - HISTORY_READINGS):end], wbgt, now["score"])
            timeline.append((base, now["level"]))
            if minute > SAMPLE_UNTIL_MINUTE:
                continue  # not enough "future" left to make a training example
            for h in random.sample(HORIZONS, HORIZONS_PER_SAMPLE):
                # Label = the HIGHEST level reached at any point in the next h minutes.
                # ("Will they reach RED within 12 min?" is more useful, and more
                # predictable, than "will they be RED at exactly minute 12?")
                future = max(LEVEL_NUM[scores[minute + k]["level"]] for k in range(1, h + 1))
                rows.append((base + [h], future, LEVEL_NUM[now["level"]]))
        timelines.append(timeline)
    return rows, timelines


def warning_lead_times(model, bar, timelines):
    """
    Replay unseen shifts minute by minute, like the live system does.
    For every worker who turned RED: did the AI warn them beforehand, and how early?
    For workers who never turned RED: did the AI raise a false alarm?
    """
    red = LEVEL_NUM["RED"]
    red_col = list(model.classes_).index(red)
    lead_times, missed, false_alarms, never_red = [], 0, 0, 0

    for timeline in timelines:
        levels = [lvl for _, lvl in timeline]
        # Ask the model about every horizon at every minute (one big batch for speed).
        rows = [base + [h] for base, _ in timeline for h in HORIZONS]
        p_red = model.predict_proba(rows)[:, red_col].reshape(len(timeline), len(HORIZONS))
        warned_at = [m for m in range(len(timeline))
                     if levels[m] != "RED" and p_red[m].max() >= bar]

        if "RED" in levels[1:]:
            first_red = levels.index("RED", 1)
            early = [m for m in warned_at if first_red - 15 <= m < first_red]
            if early:
                lead_times.append(first_red - early[0])
            else:
                missed += 1
        else:
            never_red += 1
            false_alarms += bool(warned_at)

    warned = len(lead_times)
    total_red = warned + missed
    return {
        "workers_who_turned_red": total_red,
        "warned_in_advance_pct": round(100 * warned / max(total_red, 1), 1),
        "average_warning_minutes": round(sum(lead_times) / max(warned, 1), 1),
        "workers_never_red": never_red,
        "false_alarm_pct": round(100 * false_alarms / max(never_red, 1), 1),
    }


def evaluate(model, rows):
    X = [r[0] for r in rows]
    y_true = [r[1] for r in rows]
    y_now = [r[2] for r in rows]
    y_pred = list(model.predict(X))
    n = len(rows)

    model_acc = sum(p == t for p, t in zip(y_pred, y_true)) / n
    baseline_acc = sum(c == t for c, t in zip(y_now, y_true)) / n

    # The cases that matter most: worker is NOT red now but WILL be red.
    red = LEVEL_NUM["RED"]
    became_red = [i for i in range(n) if y_now[i] != red and y_true[i] == red]
    warned = [i for i in range(n) if y_now[i] != red and y_pred[i] == red]
    caught = [i for i in became_red if y_pred[i] == red]
    recall = len(caught) / len(became_red) if became_red else 0.0
    precision = len(caught) / len(warned) if warned else 0.0

    # Trade-off: a lower confidence bar catches more future REDs but gives
    # more false alarms. Try several bars and see.
    red_col = list(model.classes_).index(red)
    p_red = model.predict_proba(X)[:, red_col]
    tradeoff = []
    for bar in (0.2, 0.25, 0.3, 0.35, 0.4, 0.5):
        warned = [i for i in range(n) if y_now[i] != red and p_red[i] >= bar]
        caught_n = sum(1 for i in warned if y_true[i] == red)
        tradeoff.append({
            "confidence_bar": bar,
            "future_red_caught_pct": round(100 * caught_n / max(len(became_red), 1), 1),
            "warnings_correct_pct": round(100 * caught_n / max(len(warned), 1), 1),
        })

    return {
        "test_examples": n,
        "model_accuracy": round(model_acc, 3),
        "baseline_no_change_accuracy": round(baseline_acc, 3),
        "future_red_cases": len(became_red),
        "future_red_caught_pct": round(100 * recall, 1),
        "red_warnings_correct_pct": round(100 * precision, 1),
        "confidence_tradeoff": tradeoff,
    }


def choose_confidence_bar(tradeoff, min_correct_pct=60):
    """Lowest bar where at least 60% of warnings are right (safety first, but not crying wolf)."""
    for row in tradeoff:  # sorted from lowest bar to highest
        if row["warnings_correct_pct"] >= min_correct_pct:
            return row
    return tradeoff[-1]


def main():
    start = time.time()
    print(f"1) Simulating {NUM_SHIFTS} practice shifts of {SHIFT_MINUTES} minutes...")
    train_rows, test_rows, test_timelines = [], [], []
    for s in range(NUM_SHIFTS):
        rows, timelines = run_shift(s)
        if s >= NUM_SHIFTS - TEST_SHIFTS:
            test_rows.extend(rows)
            test_timelines.extend(timelines)
        else:
            train_rows.extend(rows)
        if (s + 1) % 25 == 0:
            print(f"   {s + 1}/{NUM_SHIFTS} shifts done")
    print(f"   {len(train_rows):,} training examples, {len(test_rows):,} test examples")

    print("2) Training the random forest...")
    model = RandomForestClassifier(
        n_estimators=60,       # 60 decision trees that vote
        max_depth=12,          # each tree asks at most 12 questions
        min_samples_leaf=25,   # each answer must be based on 25+ examples (avoids memorising)
        n_jobs=-1,             # use all CPU cores
        random_state=0,
    )
    model.fit([r[0] for r in train_rows], [r[1] for r in train_rows])

    print("3) Testing on shifts the model has never seen...")
    metrics = evaluate(model, test_rows)
    chosen = choose_confidence_bar(metrics["confidence_tradeoff"])
    metrics["chosen_confidence_bar"] = chosen
    metrics["early_warning"] = warning_lead_times(model, chosen["confidence_bar"], test_timelines)
    metrics["feature_importance"] = {
        name: round(float(imp), 3)
        for name, imp in sorted(zip(FEATURE_NAMES, model.feature_importances_),
                                key=lambda x: -x[1])
    }

    MODEL_FILE.parent.mkdir(exist_ok=True)
    with open(MODEL_FILE, "wb") as f:
        pickle.dump({"model": model, "info": {
            "features": FEATURE_NAMES,
            "levels": LEVELS,
            "confidence_bar": chosen["confidence_bar"],
        }}, f)
    (MODEL_FILE.parent / "metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")

    print("\n=== Results on unseen test shifts ===")
    print(f"Model accuracy:                 {metrics['model_accuracy']:.1%}")
    print(f"'Nothing changes' guess:        {metrics['baseline_no_change_accuracy']:.1%}")
    print(f"Future RED cases caught early:  {metrics['future_red_caught_pct']}% "
          f"(of {metrics['future_red_cases']:,} cases)")
    print(f"RED warnings that were right:   {metrics['red_warnings_correct_pct']}%")
    print("\nConfidence bar trade-off (warn about RED when the model is at least X% sure):")
    for row in metrics["confidence_tradeoff"]:
        mark = "  <- chosen" if row is chosen else ""
        print(f"  {row['confidence_bar']:.0%} sure: catches {row['future_red_caught_pct']:>5}% "
              f"of future REDs, {row['warnings_correct_pct']:>5}% of warnings right{mark}")
    ew = metrics["early_warning"]
    print("\nEarly-warning test (replaying unseen shifts minute by minute):")
    print(f"  Workers who turned RED:          {ew['workers_who_turned_red']}")
    print(f"  ...warned in advance:            {ew['warned_in_advance_pct']}%")
    print(f"  ...average warning time:         {ew['average_warning_minutes']} minutes")
    print(f"  Workers who never turned RED:    {ew['workers_never_red']}")
    print(f"  ...given a false RED alarm:      {ew['false_alarm_pct']}%")
    print("\nWhich clues the model relies on most:")
    for name, imp in metrics["feature_importance"].items():
        print(f"  {name:<22} {'#' * round(imp * 50)} {imp:.0%}")
    size_mb = MODEL_FILE.stat().st_size / 1e6
    print(f"\nSaved model to {MODEL_FILE} ({size_mb:.1f} MB) in {time.time() - start:.0f}s")


if __name__ == "__main__":
    main()
