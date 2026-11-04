"""
AI risk forecast: shared code used by train_model.py (training) and
risk_engine.py (live predictions).

The model answers one question:
    "Given how this worker looks right now and over the last few minutes,
     what is the highest risk level they will reach within the next H minutes?"

Keeping the feature code in one place guarantees training and live
predictions calculate the clues in exactly the same way.
"""

import pickle
from pathlib import Path

MODEL_FILE = Path(__file__).resolve().parent / "models" / "risk_model.pkl"

READINGS_PER_MINUTE = 12     # one reading every 5 seconds
HISTORY_READINGS = 72        # the model looks back 6 minutes
HORIZONS = list(range(1, 16))  # forecast 1, 2, ... 15 minutes ahead

LEVELS = ["GREEN", "YELLOW", "RED"]
LEVEL_NUM = {name: i for i, name in enumerate(LEVELS)}

# Names of the clues the model sees, in order. Shown in the training report.
FEATURE_NAMES = [
    "heart_rate_now",        # average heart rate, last 1 minute
    "skin_temp_now",         # average skin temperature, last 1 minute
    "heart_rate_change_5m",  # how much heart rate rose/fell vs 5 minutes ago
    "skin_temp_change_5m",   # how much skin temp rose/fell vs 5 minutes ago
    "hard_work_share_5m",    # share of last 5 min spent on moderate/heavy work
    "rest_share_5m",         # share of last 5 min spent resting
    "wbgt",                  # heat stress of the weather
    "current_score",         # today's risk points (0-6)
    "minutes_ahead",         # how far into the future we're asking about
]


def _avg(values):
    return sum(values) / len(values)


def extract_features(readings, wbgt, current_score):
    """
    Turn a worker's recent readings (oldest first, up to 6 minutes) into
    the base clues. 'minutes_ahead' is added separately for each horizon.
    """
    now = readings[-READINGS_PER_MINUTE:]
    # The minute from 5 minutes ago. If we don't have that much history yet,
    # use the oldest minute we have (so the change is smaller, not missing).
    if len(readings) >= HISTORY_READINGS:
        before = readings[-HISTORY_READINGS:-HISTORY_READINGS + READINGS_PER_MINUTE]
    else:
        before = readings[:READINGS_PER_MINUTE]
    last_5m = readings[-5 * READINGS_PER_MINUTE:]

    hr_now = _avg([r["heart_rate_bpm"] for r in now])
    skin_now = _avg([r["skin_temp_c"] for r in now])
    hr_before = _avg([r["heart_rate_bpm"] for r in before])
    skin_before = _avg([r["skin_temp_c"] for r in before])
    activities = [r["activity_level"] for r in last_5m]

    return [
        hr_now,
        skin_now,
        hr_now - hr_before,
        skin_now - skin_before,
        sum(a in ("moderate", "heavy") for a in activities) / len(activities),
        activities.count("resting") / len(activities),
        wbgt,
        current_score,
    ]


class Predictor:
    """Loads the trained model and turns its output into a simple forecast."""

    def __init__(self, model_file=MODEL_FILE):
        with open(model_file, "rb") as f:
            saved = pickle.load(f)
        self.model = saved["model"]
        self.info = saved.get("info", {})
        # Only warn when the model is at least this confident.
        # train_model.py picks this value by testing on unseen shifts.
        self.confidence = self.info.get("confidence_bar", 0.5)

    def forecast(self, readings, wbgt, current_level, current_score):
        """
        Return a forecast dict, or None if risk isn't expected to rise.

        We ask the model about every minute from 1 to 15 and report the
        EARLIEST minute where a higher level becomes likely.
        """
        if current_level == "RED":
            return None  # already at the top level

        base = extract_features(readings, wbgt, current_score)
        rows = [base + [h] for h in HORIZONS]
        probabilities = self.model.predict_proba(rows)  # one row per horizon
        classes = list(self.model.classes_)             # e.g. [0, 1, 2]

        def chance_at_least(row, level_num):
            return sum(p for c, p in zip(classes, row) if c >= level_num)

        current_num = LEVEL_NUM[current_level]
        # Check for RED first (most important), then YELLOW.
        for target in range(len(LEVELS) - 1, current_num, -1):
            for h, row in zip(HORIZONS, probabilities):
                chance = chance_at_least(row, target)
                if chance >= self.confidence:
                    return {
                        "level": LEVELS[target],
                        "minutes": h,
                        "probability": round(float(chance), 2),
                    }
        return None


def load_predictor():
    """Return a Predictor, or None (with a message) if it can't be loaded."""
    if not MODEL_FILE.exists():
        print("  (AI forecast off: no trained model. Run: python train_model.py)")
        return None
    try:
        return Predictor()
    except Exception as error:  # e.g. scikit-learn not installed
        print(f"  (AI forecast off: could not load model: {error})")
        return None
