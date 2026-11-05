"""
Resilia AI agent: acts on heat-stress risk by itself, within set rules.

It runs inside the risk engine and, every cycle:
  1. SENSES   each worker's trend (heart rate, forehead temperature, activity)
  2. PREDICTS using the engine's AI forecast and alerts
  3. ACTS     on small, safe steps automatically (buzz the helmet, send water,
              schedule a short shade break, notify the supervisor, alert a
              first-aider on an SOS)
  4. ASKS     a human before big steps (pausing a whole crew, calling 999)
  5. CHECKS   the outcome 10 simulated minutes later and closes the case

Everything it does is written to data/agent.jsonl (shown in the action logs)
and data/agent_state.json (the AI agent page). Its actions take effect in
the simulator through data/controls.json (breaks, water, crew pauses).

This is a rule-based agent that uses the machine-learning forecast; the
rules below say exactly what it may do on its own.
"""

import json
import os
import statistics
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

DATA = Path("data")
AGENT_LOG = DATA / "agent.jsonl"          # every step the agent takes
STATE_FILE = DATA / "agent_state.json"    # snapshot for the AI agent page
CONTROLS_FILE = DATA / "controls.json"    # read by the simulator
ACTIONS_FILE = DATA / "actions.jsonl"     # supervisor actions and decisions (from the dashboard)

# What the agent may do. "auto" = does it by itself; "ask" = asks a human first.
RULES = [
    {"key": "buzz", "label": "Buzz a worker's helmet", "mode": "auto"},
    {"key": "water", "label": "Send a water runner", "mode": "auto"},
    {"key": "short_break", "label": "Schedule a short shade break", "mode": "auto"},
    {"key": "notify", "label": "Notify the supervisor", "mode": "auto"},
    {"key": "first_aider", "label": "Alert the nearest first-aider on a help call", "mode": "auto"},
    {"key": "pause_crew", "label": "Pause heavy work for a whole crew", "mode": "ask"},
    {"key": "call_999", "label": "Call emergency services", "mode": "ask"},
]
AUTO = {r["key"] for r in RULES if r["mode"] == "auto"}

BREAK_MIN = 15           # length of a short shade break
WATER_MIN = 30           # how long a water run helps
CHECK_AFTER_MIN = 10     # check the outcome this long after acting
PROPOSAL_EXPIRES_MIN = 20
OVERRIDE_COOLDOWN_MIN = 30   # after "keep working", don't ask again for this long
PEAK_END_HOUR = 15.5         # pauses run until the end of peak heat (15:30) when possible
HEAVY_LIMIT = 26.0           # WBGT limit for heavy work
ZONE_ETA = {"A": 3, "B": 4, "C": 5, "D": 6}   # water runner minutes to each zone


def iso(dt):
    return dt.isoformat(timespec="seconds")


def hhmm(dt):
    return dt.strftime("%H:%M")


class Agent:
    def __init__(self):
        self.cases = {}               # case_id -> case
        self.open_case = {}           # worker_id -> open case_id
        self.proposals = []           # big steps waiting for (or decided by) a human
        self.controls = {"breaks": {}, "water": {}, "pauses": {}}
        self.trail = defaultdict(list)   # worker_id -> [(sim_dt, hr, skin, activity)], 1 per sim minute
        self.override_until = {}         # site -> sim time until which we won't ask again
        # Start after any actions already in the log (from earlier runs), so old
        # shade breaks or approvals aren't applied again.
        try:
            with ACTIONS_FILE.open(encoding="utf-8") as f:
                self.actions_seen = sum(1 for _ in f)
        except OSError:
            self.actions_seen = 0
        self.next_case = 1
        self.next_proposal = 1
        self.started_sim = None
        self.danger_cases = set()        # case ids where the worker reached RED
        self.alert_case = {}             # alert_id -> case_id (each alert handled once)
        self.act_delays = []             # real seconds from alert to the agent's first action

    # ------------------------------------------------------------ main step
    def step(self, results, alerts, sites, sim_time):
        now = datetime.fromisoformat(sim_time)
        self.started_sim = self.started_sim or now
        workers = {r["worker_id"]: r for r in results}

        self._record_trail(results, now)
        self._read_supervisor_actions(now, workers)

        for alert in alerts:
            w = workers.get(alert["worker_id"])
            if w:
                self._handle_alert(alert, w, now)

        self._check_outcomes(workers, now)
        self._propose_crew_pauses(workers, sites, now)
        self._expire_proposals(now)
        self._write_controls(now)
        state = self._state(workers, now)
        _write_json(STATE_FILE, state)
        return {"active": True, "watching": len(workers),
                "waiting": sum(1 for p in self.proposals if p["status"] == "waiting")}

    # --------------------------------------------------------------- sensing
    def _record_trail(self, results, now):
        minute = now.replace(second=0)
        for r in results:
            t = self.trail[r["worker_id"]]
            point = (minute, r["avg_heart_rate_bpm"], r["avg_skin_temp_c"], r["activity_level"])
            if t and t[-1][0] == minute:
                t[-1] = point
            else:
                t.append(point)
                del t[:-40]   # keep 40 simulated minutes

    def _trend_text(self, w):
        """'Heart rate up from 118 to 141 bpm over 18 min during heavy work; forehead 37.0 °C.'"""
        trail = self.trail[w["worker_id"]]
        latest = trail[-1][0] if trail else None
        t = [p for p in trail if latest - p[0] <= timedelta(minutes=20)] if trail else []
        hr_now = w["avg_heart_rate_bpm"]
        doing = {"resting": "while resting", "light": "during light work",
                 "moderate": "during moderate work", "heavy": "during heavy work"}.get(w["activity_level"], "")
        if len(t) >= 2:
            low = min(t, key=lambda p: p[1])
            mins = round((t[-1][0] - low[0]).total_seconds() / 60)
            if hr_now - low[1] >= 8 and mins >= 1:
                return (f"Heart rate up from {low[1]} to {hr_now} bpm over {mins} min {doing}; "
                        f"forehead temp {w['avg_skin_temp_c']:.1f} °C.")
        return f"Heart rate {hr_now} bpm {doing}; forehead temp {w['avg_skin_temp_c']:.1f} °C; site WBGT {w['wbgt_c']:.1f} °C."

    # ----------------------------------------------------------------- cases
    def _open(self, w, now, kind):
        case_id = f"C{self.next_case}"
        self.next_case += 1
        case = {
            "case_id": case_id, "worker_id": w["worker_id"], "site": w["site"], "job": w["job"],
            "zone": w.get("zone"), "kind": kind, "status": "open", "opened_sim": iso(now),
            "alert_ids": [], "steps": [], "check_due": None, "acted": False, "reached_red": False,
            "start_hr": w["avg_heart_rate_bpm"],
        }
        self.cases[case_id] = case
        self.open_case[w["worker_id"]] = case_id
        return case

    def _step(self, case, now, kind, title, detail, action=None, alert_id=None):
        entry = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "sim_time": iso(now),
            "type": "agent",
            "kind": kind,                 # sense / predict / act / ask / check / note
            "title": title,
            "detail": detail,
            "action": action,
            "worker_id": case["worker_id"] if case else None,
            "case_id": case["case_id"] if case else None,
            "alert_id": alert_id or (case["alert_ids"][-1] if case and case["alert_ids"] else None),
            "actor": "agent",
        }
        if case:
            case["steps"].append(entry)
        DATA.mkdir(exist_ok=True)
        with AGENT_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        print(f"  AGENT: {title} ({entry['worker_id'] or '-'})")
        return entry

    def _act(self, case, w, now, alert, include_break=True):
        """The automatic first response for an at-risk worker."""
        wid, aid = w["worker_id"], alert["alert_id"]
        if "buzz" in AUTO:
            self._step(case, now, "act", "Alerted the worker",
                       f"Helmet buzzed the “go to shade” pattern on {wid}'s helmet.", "buzz", aid)
        if "water" in AUTO and not self._active(self.controls["water"], wid, now):
            eta = ZONE_ETA.get(w.get("zone") or "B", 4)
            self.controls["water"][wid] = iso(now + timedelta(minutes=WATER_MIN))
            self._step(case, now, "act", "Dispatched water",
                       f"Nearest runner assigned to Zone {w.get('zone') or '?'} · ETA {eta} min.", "water", aid)
        if include_break and "short_break" in AUTO and not self._active(self.controls["breaks"], wid, now):
            self.controls["breaks"][wid] = iso(now + timedelta(minutes=BREAK_MIN))
            self._step(case, now, "act", f"Scheduled a {BREAK_MIN}-min shade break",
                       f"Logged in the crew plan until {hhmm(now + timedelta(minutes=BREAK_MIN))} and shown to the supervisor.",
                       "short_break", aid)
        if not case["acted"]:
            case["acted"] = True
            try:
                self.act_delays.append(max(0, time.time() - datetime.fromisoformat(alert["started_at"]).timestamp()))
            except (KeyError, ValueError):
                pass
        case["check_due"] = iso(now + timedelta(minutes=CHECK_AFTER_MIN))

    def _handle_alert(self, alert, w, now):
        wid, aid, kind = w["worker_id"], alert["alert_id"], alert["kind"]
        first_time = aid not in self.alert_case
        case = self.cases.get(self.alert_case.get(aid)) or self.cases.get(self.open_case.get(wid))
        new = False
        if case is None or case["status"] == "closed":
            # An alert stays open briefly after its case closed: ignore it, unless
            # it has just turned RED (then it's a new situation).
            if not first_time and not (kind == "red" and case and not case["reached_red"]):
                return
            case = self._open(w, now, kind)
            new = True
        if first_time:
            self.alert_case[aid] = case["case_id"]
            case["alert_ids"].append(aid)

        if kind == "sos":
            if first_time:
                self._step(case, now, "sense", "Received a help call",
                           f"{wid} held the helmet SOS button. {self._trend_text(w)}", alert_id=alert["alert_id"])
                if "notify" in AUTO:
                    self._step(case, now, "act", "Notified the supervisor",
                               "Help-request banner raised in the control room.", "notify", alert["alert_id"])
                if "first_aider" in AUTO:
                    self._step(case, now, "act", "Alerted the nearest first-aider",
                               f"First-aider sent the helmet's location: {w['site']}, Zone {w.get('zone') or '?'}.",
                               "first_aider", alert["alert_id"])
                case["acted"] = True
                case["check_due"] = iso(now + timedelta(minutes=CHECK_AFTER_MIN))
            # Not moving, or in the danger zone: ask before calling 999.
            if (alert.get("motionless_s") or 0) >= 30 or w["level"] == "RED":
                self._propose_999(case, w, now, alert)
            return

        if alert["kind"] == "predicted_red" and first_time:
            self._step(case, now, "sense", "Sensed a rising trend", self._trend_text(w), alert_id=alert["alert_id"])
            f = w.get("forecast") or {}
            above = "above" if w["wbgt_c"] > w["wbgt_limit"] else "close to"
            self._step(case, now, "predict", f"Predicted heat stress in ~{f.get('minutes', alert.get('minutes', '?'))} min",
                       f"Model confidence {round((f.get('probability') or alert.get('probability') or 0) * 100)}%. "
                       f"Site WBGT {w['wbgt_c']:.1f} °C is {above} the {w['wbgt_limit']:.0f} °C limit for {w['activity_level']} work.",
                       alert_id=alert["alert_id"])
            self._act(case, w, now, alert)
            return

        if alert["kind"] == "red" and not case["reached_red"]:
            case["reached_red"] = True
            self.danger_cases.add(case["case_id"])
            if new or not case["acted"]:
                self._step(case, now, "sense", "Detected the danger zone", self._trend_text(w), alert_id=alert["alert_id"])
                self._act(case, w, now, alert)
            else:
                self._step(case, now, "note", "Reached the danger zone despite early action",
                           f"Score {w['score']}/6: " + ("; ".join(w["reasons"]) or "high heat stress") + ".",
                           alert_id=alert["alert_id"])
            if "notify" in AUTO:
                self._step(case, now, "act", "Notified the supervisor",
                           f"{wid} flagged “act now” in the control room.", "notify", alert["alert_id"])
            case["check_due"] = iso(now + timedelta(minutes=CHECK_AFTER_MIN))

    def _check_outcomes(self, workers, now):
        for case_id, case in list(self.cases.items()):
            if case["status"] != "open" or not case["check_due"]:
                continue
            if now < datetime.fromisoformat(case["check_due"]):
                continue
            w = workers.get(case["worker_id"])
            if not w:
                continue
            if w.get("sos_pressed_at") or w["status"] == "act":
                self._step(case, now, "note", "Still at risk",
                           f"{CHECK_AFTER_MIN} min later {w['worker_id']} is still “act now” "
                           f"({w['avg_heart_rate_bpm']} bpm). Supervisor notified again; checking again in {CHECK_AFTER_MIN} min.")
                case["check_due"] = iso(now + timedelta(minutes=CHECK_AFTER_MIN))
            else:
                status = {"steady": "steady", "rising": "rising, but out of the danger window"}[w["status"]]
                self._step(case, now, "check", "Checked the outcome",
                           f"Heart rate {'back to' if w['avg_heart_rate_bpm'] < case['start_hr'] else 'at'} "
                           f"{w['avg_heart_rate_bpm']} bpm, status {status}. Case closed.")
                case["status"] = "closed"
                case["closed_sim"] = iso(now)
                self.open_case.pop(case["worker_id"], None)

    # ------------------------------------------------- asking a human first
    def _propose_crew_pauses(self, workers, sites, now):
        if "pause_crew" not in {r["key"] for r in RULES}:
            return
        by_site = defaultdict(list)
        for w in workers.values():
            by_site[w["site"]].append(w)
        for site, crew in by_site.items():
            at_risk = [w for w in crew if w["status"] in ("act", "rising")]
            wbgt = (sites.get(site) or {}).get("wbgt_c", 0)
            busy = any(p["site"] == site and p["kind"] == "pause_crew" and p["status"] == "waiting" for p in self.proposals)
            paused = self._active(self.controls["pauses"], site, now)
            cooling = site in self.override_until and now < self.override_until[site]
            if len(at_risk) < 2 or wbgt <= HEAVY_LIMIT or busy or paused or cooling:
                continue
            hour = now.hour + now.minute / 60
            until = now.replace(hour=15, minute=30, second=0) if hour < PEAK_END_HOUR - 0.5 else now + timedelta(minutes=60)
            hist = (sites.get(site) or {}).get("history") or []
            rising = len(hist) >= 10 and hist[-1][1] > hist[-10][1]
            proposal = {
                "proposal_id": f"P{self.next_proposal}", "kind": "pause_crew", "site": site,
                "status": "waiting", "created_sim": iso(now), "until": iso(until),
                "title": f"Pause heavy work for the {site} crew until {hhmm(until)}?",
                "affects": len(crew),
                "why": f"This affects {len(crew)} workers, so the agent won't decide alone.",
                "reasons": [
                    f"WBGT {wbgt:.1f} °C{' and still rising' if rising else ''}: above the {HEAVY_LIMIT:.0f} °C heavy-work limit",
                    f"{len(at_risk)} of {len(crew)} crew members trending toward risk ({', '.join(w['worker_id'] for w in at_risk)})",
                    "Light tasks remain allowed in the shaded area",
                ],
                "approve_label": "Approve pause", "override_label": "Override · keep working",
                "workers": [w["worker_id"] for w in crew],
            }
            self.next_proposal += 1
            self.proposals.append(proposal)
            for w in at_risk:
                case = self.cases.get(self.open_case.get(w["worker_id"]))
                if case:
                    self._step(case, now, "ask", "Asked for approval on a bigger step",
                               f"Pausing heavy work for the whole {site} crew needs a human decision.")

    def _propose_999(self, case, w, now, alert):
        if any(p["kind"] == "call_999" and p.get("case_id") == case["case_id"] for p in self.proposals):
            return
        proposal = {
            "proposal_id": f"P{self.next_proposal}", "kind": "call_999", "site": w["site"],
            "case_id": case["case_id"], "worker_id": w["worker_id"], "status": "waiting", "created_sim": iso(now),
            "title": f"Call emergency services (999) for {w['worker_id']}?",
            "affects": 1,
            "why": "Calling emergency services is always a human decision.",
            "reasons": [
                f"{w['worker_id']} held the SOS button and " +
                (f"hasn't moved for {alert.get('motionless_s')} s" if (alert.get("motionless_s") or 0) >= 30 else "is in the danger zone"),
                f"Last vitals: {w['avg_heart_rate_bpm']} bpm, forehead {w['avg_skin_temp_c']:.1f} °C",
                "The first-aider has already been alerted",
            ],
            "approve_label": "Approve · I'll call 999", "override_label": "Override · first-aider handles it",
            "workers": [w["worker_id"]],
        }
        self.next_proposal += 1
        self.proposals.append(proposal)
        self._step(case, now, "ask", "Asked for approval on a bigger step",
                   "Calling emergency services needs a human decision.")

    def _expire_proposals(self, now):
        for p in self.proposals:
            if p["status"] == "waiting" and now - datetime.fromisoformat(p["created_sim"]) > timedelta(minutes=PROPOSAL_EXPIRES_MIN):
                p["status"] = "expired"
                p["decided_sim"] = iso(now)
                if p["kind"] == "pause_crew":  # don't immediately ask the same thing again
                    self.override_until[p["site"]] = now + timedelta(minutes=OVERRIDE_COOLDOWN_MIN)
                self._step(None, now, "note", f"No decision on: {p['title']}", "Request expired; the agent keeps watching.")

    # ----------------------------------------- supervisor actions & decisions
    def _read_supervisor_actions(self, now, workers):
        """Apply what supervisors did in the dashboard (shade breaks, water, approvals)."""
        entries = []
        try:
            with ACTIONS_FILE.open(encoding="utf-8") as f:
                for line in f:
                    try:
                        entries.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            return
        new, self.actions_seen = entries[self.actions_seen:], len(entries)
        for e in new:
            wid = e.get("worker_id")
            if e.get("action") == "shade_break" and wid:
                self.controls["breaks"][wid] = iso(now + timedelta(minutes=BREAK_MIN))
            elif e.get("action") == "dispatch_water" and wid:
                self.controls["water"][wid] = iso(now + timedelta(minutes=WATER_MIN))
            elif e.get("action") in ("approve", "override") and e.get("proposal_id"):
                self._decide(e["proposal_id"], e["action"], now)

    def _decide(self, proposal_id, decision, now):
        p = next((x for x in self.proposals if x["proposal_id"] == proposal_id), None)
        if not p or p["status"] != "waiting":
            return
        p["status"] = "approved" if decision == "approve" else "overridden"
        p["decided_sim"] = iso(now)
        if p["kind"] == "pause_crew":
            if decision == "approve":
                self.controls["pauses"][p["site"]] = p["until"]
                title = f"Supervisor approved: heavy work paused at {p['site']} until {hhmm(datetime.fromisoformat(p['until']))}"
                detail = "Light tasks continue in the shaded area. The agent keeps watching."
            else:
                self.override_until[p["site"]] = now + timedelta(minutes=OVERRIDE_COOLDOWN_MIN)
                title = f"Supervisor overrode: {p['site']} crew keeps working"
                detail = f"The agent keeps watching and won't ask again for {OVERRIDE_COOLDOWN_MIN} min."
        else:
            if decision == "approve":
                title = f"Supervisor approved the 999 call for {p['worker_id']}"
                detail = "The supervisor dials 999 (Resilia doesn't place calls). First-aider stays with the worker."
            else:
                title = f"Supervisor decided not to call 999 for {p['worker_id']}"
                detail = "The first-aider handles it; the agent keeps watching."
        cases = [c for c in self.cases.values() if c["status"] == "open" and c["worker_id"] in p["workers"]]
        if cases:
            for c in cases:
                self._step(c, now, "check" if decision == "approve" else "note", title, detail)
        else:
            self._step(None, now, "note", title, detail)

    # ---------------------------------------------------------------- output
    @staticmethod
    def _active(table, key, now):
        until = table.get(key)
        return bool(until) and datetime.fromisoformat(until) > now

    def _write_controls(self, now):
        for table in self.controls.values():
            for key in [k for k, v in table.items() if datetime.fromisoformat(v) <= now]:
                del table[key]
        _write_json(CONTROLS_FILE, self.controls)

    def _state(self, workers, now):
        cases = sorted(self.cases.values(), key=lambda c: c["opened_sim"], reverse=True)
        auto_cases = [c for c in cases if c["acted"]]
        delays = self.act_delays
        return {
            "sim_time": iso(now),
            "shift_started_sim": iso(self.started_sim),
            "watching": len(workers),
            "rules": RULES,
            "kpis": {
                "handled_automatically": len(auto_cases),
                "median_seconds_to_act": round(statistics.median(delays), 1) if delays else None,
                "reached_danger": len(self.danger_cases),
                "cases": len(cases),
                "waiting": sum(1 for p in self.proposals if p["status"] == "waiting"),
            },
            "proposals": self.proposals[-12:],
            "cases": cases[:20],
            "controls": self.controls,
        }


def _write_json(path, data):
    path.parent.mkdir(exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    for _ in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:  # Windows: being read right now
            time.sleep(0.05)
