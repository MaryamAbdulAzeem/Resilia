# Resilia: Heat-Stress Monitoring Prototype

Hackathon prototype for monitoring heat stress in outdoor workers in the UAE,
using (simulated) Resilia smart helmets. Design mock-ups are in `designs/`.

## What's here

| File | What it does |
|------|--------------|
| `simulator.py` | Pretends to be 15 workers wearing Resilia helmets. Every 5 seconds each helmet sends heart rate, forehead skin temperature, activity, solar battery %, gateway, and occasionally an SOS button press. Has its own clock (summer air and sun follow the time of day). |
| `risk_engine.py` | Reads the readings, gets live weather from Open-Meteo, estimates WBGT, rates each worker GREEN / YELLOW / RED and Steady / Rising / Act now, and raises heat and SOS alerts. |
| `dashboard_server.py` | Small web server for the dashboard and its data. |
| `dashboard/index.html` | Control room page (SOS banner, AI alert card, crew table, action log, map). |
| `dashboard/assets/` | Shared Resilia styles and page engine (header, heat arc, live data, sound, actions). |
| `run_all.py` | Starts the simulator, risk engine and dashboard together. |
| `predictor.py` | AI forecast: turns readings into clues and asks the model about the next 15 minutes. |
| `train_model.py` | Trains the AI forecast on simulated shifts and tests it. |
| `models/metrics.json` | Test results from the last training run. |
| `data/readings.jsonl` | Created when you run the simulator. One reading per line, in JSON format. |
| `data/risk_scores.json` | Created by the risk engine. Latest score for every worker. |

## Requirements

Python 3.9 or newer. No extra packages needed.

## How to run

```
python simulator.py
```

Press `Ctrl+C` to stop.

Useful options:

```
python simulator.py --interval 1      # faster: a reading every 1 second
python simulator.py --rounds 10       # stop after 10 rounds
python simulator.py --seed 42         # same "random" data every run (good for demos)
```

## Quick start: everything at once

```
python -m pip install --user -r requirements.txt   # first time only (for the AI forecast)
python train_model.py                              # first time only (~30 seconds)
python run_all.py
```

Then open **http://localhost:8000**. Press `Ctrl+C` to stop. Logs go to `data/logs/`.

**For a live demo**, use:

```
python run_all.py --demo
```

Time runs 15x faster, the simulated clock starts at 11:30, the dashboard starts
on the summer scenario, and the story is nearly the same every run: W02 presses
SOS at ~20-30 seconds, and the AI agent catches most at-risk workers (like W08)
early (see "AI agent" below).
Forecasts show both simulated minutes and real seconds. `--speed 5` gives a
custom speed.

The **control room** (Resilia design: navy-dusk header with the sunrise-to-sunset
heat arc, sand background, sun-gold accents, coral only for at-risk, sage only
for steady; Space Grotesk headings, Inter body) shows:
- the **SOS help-request banner** (On my way / Send first-aider / Call 999),
- the most urgent **AI alert card** (Dispatch water / Schedule shade break /
  Buzz again / Show location), with chips to switch between other alerts,
- KPI tiles, live **site conditions** with a WBGT trend, the **crew table**
  (tap a row for history and per-worker actions), the **action log**, helmet
  solar battery, and a **crew map**.

Status words: **Act now** = SOS, RED or predicted RED · **Rising** = body signs
of strain or a rising forecast · **Steady** = everything else (a worker who is
YELLOW only because the site is hot shows as Steady). Every status shows a word
and a dot, so colour is never the only signal. "Call 999" only records the
decision: the supervisor dials 999 themselves.
It refreshes every 3 seconds. The switch at the top flips between **Live weather**
(Open-Meteo) and **Summer scenario** (simulated UAE summer afternoon). The map needs
internet; cards work without it.

## AI agent (`agent.py`, page: AI agent)

Runs inside the risk engine. For every heat or SOS alert it opens a **case** and
follows sense → predict → act → check:

| Action | Mode |
|---|---|
| Buzz the worker's helmet, send a water runner, schedule a 15-min shade break, notify the supervisor, alert a first-aider on SOS | **Automatic** |
| Pause heavy work for a whole site crew, call emergency services (999) | **Asks first** (Approve / Override on the AI agent page) |

Actions really change the simulation (via `data/controls.json`): a shade break
makes the worker rest in the shade, water slows heating up, an approved pause
stops heavy work at that site. The supervisor's own Dispatch water / Schedule
shade break buttons work the same way. The agent checks each case 10 simulated
minutes later and closes it, or flags "still at risk".

The agent's automatic steps count as handling a *predicted* alert, but RED and
SOS alerts always need a person. Requests expire after 20 simulated minutes;
after an override (or expiry) it won't ask the same crew again for 30 minutes.

**Effect on the demo:** because the agent acts early, most workers (like W08)
cool down before reaching the danger zone. W12 is predicted only about a minute
ahead, so depending on timing it sometimes still turns RED. W02's SOS arrives
at about 20-30 seconds.

## Insights (`insights.py`, page: Insights)

Built live from the shift (written to `data/insights.json`):
- **Heat-map** by site and hour: share of worker-minutes at Rising or Act now
  (a cell needs 20 worker-minutes before it's shown; the current hour is outlined).
- **Risk by job type**, for all sites or the site picked in the header.
- **Alerts vs. interventions, by hour** of this shift.
- **Recommendations** worked out from those numbers: scheduling (needs two hours
  of data), water stations, how much risk falls outside the 12:30–15:00 midday
  ban, and which crews need extra breaks. **Copy brief** copies them as text.

The design's "emergencies avoided / AED saved" and weekly figures were left out:
the prototype has no real basis for them. In demo mode one simulated hour takes
about 4 real minutes, so the heat-map fills in as the demo runs.

## Sharing a public link (view-only)

```
python run_all.py --demo --share
```

Also starts a free Cloudflare quick tunnel and prints a temporary public link
(`https://<random-words>.trycloudflare.com`). It also appears as a green
"🌐 Shared" pill on the laptop's dashboard (click to copy it).

- **View-only:** anyone opening the public link can watch everything but can't
  press action buttons or change the weather. The server refuses changes from
  the tunnel (it recognises Cloudflare's headers); only `http://localhost:8000`
  on the laptop has full control.
- **Temporary:** the link stops when you press `Ctrl+C`, and each run gets a new
  address. If the link drops, `run_all.py` makes a new one (up to 3 times) and
  prints it; the local dashboard keeps running either way.
- Needs `cloudflared`: `winget install --id Cloudflare.cloudflared -e`.
- Keep the laptop awake and online while sharing.

## Supervisor alerts and actions

- **When:** the risk engine opens an alert when a worker turns RED or the AI
  predicts RED. A predicted alert *escalates* if the worker then actually turns RED.
  An alert clears once the worker has been calm for 2 simulated minutes (so a
  worker flickering RED/YELLOW doesn't trigger a flood of alerts).
- **Pop-ups** (bottom-right) for every unhandled alert, with **Dispatch water**,
  **Schedule shade break** and **Acknowledge**. Any of these handles the alert.
  After an escalation, the supervisor has to respond again.
- **Sound:** three urgent beeps for RED, two softer beeps for predicted RED, and a
  reminder every 30 s while a RED alert is unhandled. Browsers block sound until you
  click the page once; the sound button in the header shows this.
- **Card buttons:** every card has Dispatch water / Schedule shade break and shows
  the last action taken.
- **Action log** at the bottom: alerts raised, escalated and cleared, plus every
  supervisor action with its response time. Stored in `data/alerts.jsonl`
  (risk engine) and `data/actions.jsonl` (dashboard server). `--demo` starts with
  an empty log; normal runs keep it.

Actions are recorded but don't (yet) change the simulated worker.

## Running the risk engine

Open a second terminal while the simulator is running:

```
python risk_engine.py            # score everyone once
python risk_engine.py --watch    # re-score every 5 seconds
python risk_engine.py --offline  # no internet: use simulated summer weather
```

### How scoring works

1. Average each worker's last 12 readings (1 minute) to smooth out noise.
2. Get temperature, humidity and sunlight for their site from Open-Meteo
   (cached for 10 minutes per site).
3. Estimate WBGT = 0.7 x wet bulb + 0.2 x globe + 0.1 x air temp.
   Wet bulb uses Stull's formula; globe temp is estimated from sunlight.
4. Give 0 / 1 / 2 points for each warning sign:

| Sign | 1 point | 2 points |
|------|---------|----------|
| WBGT vs. limit for activity (rest 33, light 31, moderate 28, heavy 26) | within 2 C of limit | above limit |
| Average heart rate | over 130 bpm | over 155 bpm |
| Average skin temperature | over 35.5 C | over 36.5 C |

5. Total 0-1 = GREEN, 2-3 = YELLOW, 4+ = RED.

These thresholds are prototype choices, not medically validated.

## AI risk forecast

Predicts whether a worker will reach a higher risk level in the next 15 minutes,
e.g. "Predicted: RED in ~6 min".

- **Model:** random forest (60 decision trees that vote), via scikit-learn.
- **Training data:** 150 fast-forwarded simulator shifts (75 min each, 15 workers).
  120 for training, 30 kept aside for testing.
- **Clues (features):** heart rate and skin temp (now and change over 5 min),
  share of hard work and rest in the last 5 min, WBGT, current score, minutes ahead.
- **Question it learns:** "what's the highest level this worker reaches within H minutes?"
- **Warning rule:** show the earliest minute at which the model is at least 30% sure
  of a higher level (bar picked automatically so 60%+ of RED warnings are right).

Test results (unseen shifts): 87% accuracy vs 75% for "nothing changes";
86% of workers who turned RED were warned in advance, about 6 minutes early on average;
29% of workers who never turned RED got at least one false alarm.
Re-run `python train_model.py` to see current numbers.

**Limitation:** trained on simulated data, so it learns the simulator's rules.
Real use would need data from real helmets.

## Example reading

```json
{"timestamp": "2026-11-04T10:15:00+00:00", "worker_id": "W03", "site": "Abu Dhabi",
 "job": "landscaping", "heart_rate_bpm": 128, "skin_temp_c": 36.4,
 "activity_level": "heavy", "air_temp_c": 43.2}
```

## How the simulation works

- Each worker has their own resting heart rate and heat tolerance.
- Workers W03, W08 and W12 have low heat tolerance (for example, new to the job),
  so their readings climb faster. This lets the demo show alerts.
- Activity changes gradually between resting, light, moderate and heavy.
- A hidden "heat strain" value builds up during hard work in hot air and drops
  during rest. It pushes heart rate and skin temperature up.
- Air temperature at each site (Dubai, Abu Dhabi, Sharjah, Al Ain) drifts
  between 36 and 49 C.
