"""
Shift insights for site managers, employers and regulators.

Runs inside the risk engine and builds up, from the live data:
  - a heat-map: share of worker-minutes at "rising" or "act now", by site and hour
  - risk by job type
  - alerts vs. interventions by hour
  - recommendations worked out from those numbers

Everything is calculated from this shift's simulated helmets; nothing is made up.
Written to data/insights.json for the Insights page.
"""

import json
import os
import statistics
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

INSIGHTS_FILE = Path("data") / "insights.json"
AT_RISK = ("rising", "act")
BAN_START, BAN_END = 12.5, 15.0   # UAE midday outdoor work ban: 12:30-15:00 (mid-June to mid-September)
HOURS = list(range(6, 18))        # heat-map columns: 06:00 ... 17:00 (shifts end at 18:00)
# How each job's work reads in advice ("Move heavy road works in Al Ain ...").
JOB_WORK = {
    "road works": "heavy road works",
    "construction": "heavy construction work",
    "oil & gas site": "heavy oil & gas site work",
    "landscaping": "landscaping work",
    "delivery rider": "delivery rounds",
}
MIN_MINUTES_PER_CELL = 20         # a site-hour needs this many worker-minutes to count


def _pair():
    return [0, 0]


class ShiftStats:
    def __init__(self, period="this shift"):
        self.period = period   # how recommendations describe the time span
        self.cells = defaultdict(_pair)               # (site, hour) -> [worker-minutes, at-risk worker-minutes]
        self.jobs = defaultdict(_pair)                # (site, job)  -> [worker-minutes, at-risk]
        self.wbgt = defaultdict(list)                 # (site, hour) -> WBGT samples (one per minute)
        self.last_minute = {}                         # worker -> last minute counted
        self.site_minute = {}                         # site -> last minute WBGT sampled
        self.risk_in_ban = 0
        self.risk_total = 0
        self.alerts_by_hour = Counter()
        self.interventions_by_hour = Counter()
        self.alerts_by_day = Counter()           # "2026-11-05" -> alerts that day
        self.interventions_by_day = Counter()
        self.water_by_site = Counter()
        self.warning_minutes = []                     # AI lead time (minutes) for each predicted alert
        self.predicted_ids = set()
        self.escalated_ids = set()
        self.red_alerts = 0
        self.red_warned = 0       # danger-zone alerts the AI had forecast in the 15 min before
        self.red_unwarned = 0     # danger-zone alerts with no forecast at all
        self.interventions = 0
        self.site_of = {}
        self.per_worker = defaultdict(_pair)           # worker -> [minutes on shift, minutes at risk]
        self.worker_alerts = Counter()
        self.worker_breaks = Counter()
        self.worker_water = Counter()
        self.worker_sos = Counter()
        self.first_sim = None
        self.last_sim = None

    # ----------------------------------------------------------- recording
    def update(self, results, sites, sim_time):
        now = datetime.fromisoformat(sim_time)
        minute = now.replace(second=0)
        hour = now.hour
        self.first_sim = self.first_sim or minute
        self.last_sim = minute
        for w in results:
            wid = w["worker_id"]
            self.site_of[wid] = w["site"]
            if self.last_minute.get(wid) == minute:
                continue   # one sample per worker per simulated minute
            self.last_minute[wid] = minute
            risky = w["status"] in AT_RISK
            for table, key in ((self.cells, (w["site"], hour)), (self.jobs, (w["site"], w["job"])),
                               (self.per_worker, wid)):
                table[key][0] += 1
                table[key][1] += int(risky)
            if risky:
                self.risk_total += 1
                if BAN_START <= now.hour + now.minute / 60 < BAN_END:
                    self.risk_in_ban += 1
        for site, s in sites.items():
            if self.site_minute.get(site) != minute:
                self.site_minute[site] = minute
                self.wbgt[(site, hour)].append(s["wbgt_c"])

    def on_event(self, entry, alert=None):
        """Called for every alert, agent step and supervisor action."""
        try:
            when = datetime.fromisoformat(entry.get("sim_time") or "")
        except ValueError:
            when = self.last_sim
        hour = when.hour if when else None
        day = when.date().isoformat() if when else None
        kind, etype = entry.get("kind"), entry.get("type")
        wid = entry.get("worker_id")
        if etype in ("alert", "sos") and wid:
            self.worker_alerts[wid] += 1
            if etype == "sos":
                self.worker_sos[wid] += 1
        if wid and entry.get("action") in ("water", "water_join", "dispatch_water"):
            self.worker_water[wid] += 1   # water reached this worker (own run or a shared one)
        if wid and entry.get("action") in ("short_break", "shade_break"):
            self.worker_breaks[wid] += 1
        if etype in ("alert", "sos"):
            self.alerts_by_day[day] += 1
        if etype == "alert":
            self.alerts_by_hour[hour] += 1
            if kind == "predicted_red":
                self.predicted_ids.add(entry["alert_id"])
                if alert and alert.get("minutes") is not None:
                    self.warning_minutes.append(alert["minutes"])
            elif kind == "red":
                self.red_alerts += 1
                if entry.get("warned"):
                    self.red_warned += 1     # the AI had forecast it (but no separate alert opened)
                else:
                    self.red_unwarned += 1   # came with no warning at all
        elif etype == "alert_escalated":
            self.escalated_ids.add(entry["alert_id"])
            self.red_alerts += 1
        elif etype == "sos":
            self.alerts_by_hour[hour] += 1
        intervention = (etype == "agent" and entry.get("action") in ("water", "short_break")) or \
                       (etype == "action" and entry.get("action") in ("dispatch_water", "shade_break"))
        if intervention:
            self.interventions += 1
            self.interventions_by_hour[hour] += 1
            self.interventions_by_day[day] += 1
            if entry.get("action") in ("water", "dispatch_water"):
                self.water_by_site[self.site_of.get(entry.get("worker_id"), "?")] += 1

    def merge(self, other):
        """Add another ShiftStats (e.g. another simulated day) into this one."""
        for mine, theirs in ((self.cells, other.cells), (self.jobs, other.jobs), (self.per_worker, other.per_worker)):
            for key, (m, r) in theirs.items():
                mine[key][0] += m
                mine[key][1] += r
        for key, vals in other.wbgt.items():
            self.wbgt[key].extend(vals)
        for name in ("alerts_by_hour", "interventions_by_hour", "alerts_by_day", "interventions_by_day",
                     "water_by_site", "worker_alerts", "worker_breaks", "worker_water", "worker_sos"):
            getattr(self, name).update(getattr(other, name))
        for name in ("risk_in_ban", "risk_total", "red_alerts", "red_warned", "red_unwarned", "interventions"):
            setattr(self, name, getattr(self, name) + getattr(other, name))
        self.warning_minutes.extend(other.warning_minutes)
        self.predicted_ids |= other.predicted_ids
        self.escalated_ids |= other.escalated_ids
        self.site_of.update(other.site_of)
        firsts = [t for t in (self.first_sim, other.first_sim) if t]
        lasts = [t for t in (self.last_sim, other.last_sim) if t]
        self.first_sim = min(firsts) if firsts else None
        self.last_sim = max(lasts) if lasts else None

    # ------------------------------------------------------------- results
    def share(self, cell):
        return cell[1] / cell[0] if cell[0] else None

    def recommendations(self):
        recs = []
        hours_seen = sorted({h for (_, h), c in self.cells.items() if c[0] >= MIN_MINUTES_PER_CELL})
        minutes_so_far = int((self.last_sim - self.first_sim).total_seconds() // 60) if self.first_sim else 0

        # 1) Scheduling: at the riskiest site, compare its peak hour with the calm
        #    hours BEFORE it, and suggest moving that site's riskiest job earlier.
        #    (Only jobs actually done at that site are considered.)
        if len(hours_seen) >= 2:
            best = None
            for site in {s for s, _ in self.cells}:
                shares = {h: self.share(self.cells[(site, h)]) for h in hours_seen if self.cells[(site, h)][0] >= MIN_MINUTES_PER_CELL}
                if len(shares) < 2:
                    continue
                peak_h = max(shares, key=shares.get)
                before = {h: s for h, s in shares.items() if h < peak_h} or shares
                low_h = min(before, key=before.get)
                if best is None or shares[peak_h] > best[1]:
                    best = (site, shares[peak_h], peak_h, shares[low_h], low_h, shares)
            if best and best[1] > 0:
                site, peak, peak_h, low, low_h, shares = best
                site_jobs = [j for s, j in self.jobs if s == site]
                job = max(site_jobs, key=lambda j: self.share(self.jobs[(site, j)]) or 0)
                # The first hour where risk reaches at least half of the peak: the work should finish before it.
                rise_h = next((h for h in sorted(shares) if h <= peak_h and shares[h] >= peak / 2), peak_h)
                recs.append({"type": "Scheduling", "text":
                    f"Move {JOB_WORK.get(job, job + ' work')} in {site} to before {rise_h:02d}:00: the share of worker-time "
                    f"at risk there rises from {low:.0%} at {low_h:02d}:00 to {peak:.0%} at {peak_h:02d}:00."})
        else:
            recs.append({"type": "Scheduling", "text":
                f"Collecting data: scheduling advice appears once Resilia has seen two hours of the shift "
                f"({minutes_so_far} simulated minutes so far)."})

        # 2) Resources: where water is needed most.
        site_wbgt = {s: statistics.mean(v) for s, v in self._site_wbgt().items() if v}
        hot = sorted((s for s, v in site_wbgt.items() if v > 32), key=lambda s: -site_wbgt[s])
        if hot:
            runs = sum(self.water_by_site[s] for s in hot)
            recs.append({"type": "Resources", "text":
                f"Pre-position water stations at {_join(hot)} (average WBGT above 32 °C) instead of sending runners on demand: "
                f"{runs} water run{'s' if runs != 1 else ''} went there {self.period}."})
        elif self.water_by_site:
            site, runs = self.water_by_site.most_common(1)[0]
            recs.append({"type": "Resources", "text":
                f"{site} needed the most water runs ({runs}): keep a water station there."})

        # 3) Policy: how much risk falls outside the fixed midday ban.
        if self.risk_total:
            outside = 1 - self.risk_in_ban / self.risk_total
            recs.append({"type": "Policy", "text":
                f"Use WBGT-based break rules alongside the fixed 12:30–15:00 midday work ban: "
                f"{outside:.0%} of at-risk worker-time {self.period} fell outside the ban hours."})

        # 4) People: the job type that spends most time at risk.
        jobs = self.job_shares()
        if jobs:
            avg = sum(j["at_risk"] for j in jobs) / max(1, sum(j["minutes"] for j in jobs))
            top = jobs[0]
            if avg > 0 and top["share"] > avg * 1.2:
                where = sorted(s for s, j in self.jobs if j == top["job"])
                recs.append({"type": "Crews", "text":
                    f"Give {top['job']} crews ({_join(where)}) extra shade breaks: {top['share']:.0%} of their time "
                    f"was at risk, {top['share'] / avg:.1f}× the crew average."})
        return recs

    def _site_wbgt(self):
        out = defaultdict(list)
        for (site, _), vals in self.wbgt.items():
            out[site].extend(vals)
        return out

    def job_shares(self, site=None):
        agg = defaultdict(lambda: [0, 0])
        for (s, job), (m, r) in self.jobs.items():
            if site is None or s == site:
                agg[job][0] += m
                agg[job][1] += r
        rows = [{"job": j, "minutes": m, "at_risk": r, "share": r / m if m else 0} for j, (m, r) in agg.items()]
        return sorted(rows, key=lambda x: -x["share"])

    def snapshot(self):
        sites = sorted({s for s, _ in self.cells})
        # Heat-risk episodes = every AI warning + every danger-zone alert that came
        # with no warning. "Caught early" = episodes stopped before the danger zone,
        # so it already counts the unwarned ones (they were missed).
        predicted = len(self.predicted_ids)
        escalated = len(self.escalated_ids & self.predicted_ids)
        unwarned = self.red_unwarned
        # Episodes: AI-warning alerts, plus danger-zone alerts (forecast or not).
        episodes = predicted + self.red_warned + unwarned
        caught = predicted - escalated
        reached_despite_warning = escalated + self.red_warned
        return {
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "first_sim": self.first_sim.isoformat() if self.first_sim else None,
            "last_sim": self.last_sim.isoformat() if self.last_sim else None,
            "hours": HOURS,
            "min_minutes_per_cell": MIN_MINUTES_PER_CELL,
            "heatmap": {s: {str(h): {"minutes": self.cells[(s, h)][0], "at_risk": self.cells[(s, h)][1],
                                     "wbgt": round(statistics.mean(self.wbgt[(s, h)]), 1) if self.wbgt[(s, h)] else None}
                            for h in HOURS if (s, h) in self.cells} for s in sites},
            "jobs": {s: self.job_shares(s) for s in sites},
            "by_hour": {str(h): {"alerts": self.alerts_by_hour[h], "interventions": self.interventions_by_hour[h]}
                        for h in HOURS if self.alerts_by_hour[h] or self.interventions_by_hour[h]},
            "by_day": {d: {"alerts": self.alerts_by_day[d], "interventions": self.interventions_by_day[d]}
                       for d in sorted(set(self.alerts_by_day) | set(self.interventions_by_day)) if d},
            "kpis": {
                "early_warnings": predicted,
                "median_warning_minutes": round(statistics.median(self.warning_minutes)) if self.warning_minutes else None,
                "interventions": self.interventions,
                "caught_early_pct": round(100 * caught / episodes) if episodes else None,
                "episodes": episodes,
                "caught_early": caught,
                "reached_despite_warning": reached_despite_warning,
                "danger_alerts": self.red_alerts,
                # Danger-zone alerts that came with NO earlier AI warning (e.g. a sudden
                # switch to heavy work in extreme heat). Shown so "caught early" isn't oversold.
                "unwarned_danger": unwarned,
                "at_risk_share": round(self.risk_total / max(1, sum(c[0] for c in self.cells.values())), 3),
            },
            "recommendations": self.recommendations(),
            # Per-worker shift totals (for the Workers page).
            "workers": {wid: {"minutes": m, "at_risk_minutes": r, "alerts": self.worker_alerts[wid],
                              "sos": self.worker_sos[wid], "breaks": self.worker_breaks[wid],
                              "water_runs": self.worker_water[wid]}
                        for wid, (m, r) in self.per_worker.items()},
        }

    def write(self):
        path = INSIGHTS_FILE
        path.parent.mkdir(exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.snapshot(), indent=1), encoding="utf-8")
        for _ in range(5):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                time.sleep(0.05)


DAY_FILE = Path("data") / "day_history.json"


class DayTrail:
    """
    Each worker's whole day, for the Workers page history chart: one point per
    simulated minute (heart rate, forehead temperature, site WBGT, status) and
    the alerts and interventions (water, shade breaks) along the way.
    """

    MAX_POINTS = 16 * 60   # up to 16 simulated hours

    def __init__(self, path=DAY_FILE):
        self.path = path
        self.points = defaultdict(list)   # worker -> [[HH:MM, hr, skin, wbgt, status], ...]
        self.events = defaultdict(list)   # worker -> [[HH:MM, type, label], ...]
        self.last_minute = None

    def update(self, results, sim_time):
        minute = sim_time[11:16]
        for w in results:
            row = [minute, w["avg_heart_rate_bpm"], w["avg_skin_temp_c"], w["wbgt_c"], w["status"]]
            pts = self.points[w["worker_id"]]
            if pts and pts[-1][0] == minute:
                pts[-1] = row
            else:
                pts.append(row)
                del pts[:-self.MAX_POINTS]
        if minute != self.last_minute:   # write once per simulated minute
            self.last_minute = minute
            self.write()

    EVENT_NAMES = {
        ("alert", "predicted_red"): ("alert", "AI predicted danger"),
        ("alert", "red"): ("alert", "Danger zone"),
        ("alert_escalated", None): ("alert", "Reached danger zone"),
        ("sos", None): ("sos", "SOS help request"),
    }

    def on_event(self, entry, alert=None):
        wid = entry.get("worker_id")
        if not wid:
            return
        t = (entry.get("sim_time") or "")[11:16]
        etype, action = entry.get("type"), entry.get("action")
        named = self.EVENT_NAMES.get((etype, entry.get("kind"))) or self.EVENT_NAMES.get((etype, None))
        if etype in ("alert", "alert_escalated", "sos") and named:
            self.events[wid].append([t, *named])
        elif action in ("water", "dispatch_water"):
            self.events[wid].append([t, "water", "Water run" + (" (supervisor)" if etype == "action" else "")])
        elif action == "water_join":
            self.events[wid].append([t, "water", "Water (shared run)"])
        elif action in ("short_break", "shade_break"):
            self.events[wid].append([t, "break", "Shade break" + (" (supervisor)" if etype == "action" else "")])

    def write(self):
        data = {"workers": {wid: {"points": pts, "events": self.events.get(wid, [])}
                            for wid, pts in self.points.items()}}
        self.path.parent.mkdir(exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        for _ in range(5):
            try:
                os.replace(tmp, self.path)
                return
            except PermissionError:
                time.sleep(0.05)


def _join(items):
    items = list(items)
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]
