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
HOURS = list(range(6, 20))        # heat-map columns: 06:00 ... 19:00
MIN_MINUTES_PER_CELL = 20         # a site-hour needs this many worker-minutes to count


class ShiftStats:
    def __init__(self):
        self.cells = defaultdict(lambda: [0, 0])      # (site, hour) -> [worker-minutes, at-risk worker-minutes]
        self.jobs = defaultdict(lambda: [0, 0])       # (site, job)  -> [worker-minutes, at-risk]
        self.wbgt = defaultdict(list)                 # (site, hour) -> WBGT samples (one per minute)
        self.last_minute = {}                         # worker -> last minute counted
        self.site_minute = {}                         # site -> last minute WBGT sampled
        self.risk_in_ban = 0
        self.risk_total = 0
        self.alerts_by_hour = Counter()
        self.interventions_by_hour = Counter()
        self.water_by_site = Counter()
        self.warning_minutes = []                     # AI lead time (minutes) for each predicted alert
        self.predicted_ids = set()
        self.escalated_ids = set()
        self.red_alerts = 0
        self.interventions = 0
        self.site_of = {}
        self.per_worker = defaultdict(lambda: [0, 0])  # worker -> [minutes on shift, minutes at risk]
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
            hour = datetime.fromisoformat(entry.get("sim_time") or "").hour
        except ValueError:
            hour = self.last_sim.hour if self.last_sim else None
        kind, etype = entry.get("kind"), entry.get("type")
        wid = entry.get("worker_id")
        if etype in ("alert", "sos") and wid:
            self.worker_alerts[wid] += 1
            if etype == "sos":
                self.worker_sos[wid] += 1
        if wid and entry.get("action") in ("water", "dispatch_water"):
            self.worker_water[wid] += 1
        if wid and entry.get("action") in ("short_break", "shade_break"):
            self.worker_breaks[wid] += 1
        if etype == "alert":
            self.alerts_by_hour[hour] += 1
            if kind == "predicted_red":
                self.predicted_ids.add(entry["alert_id"])
                if alert and alert.get("minutes") is not None:
                    self.warning_minutes.append(alert["minutes"])
            elif kind == "red":
                self.red_alerts += 1
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
            if entry.get("action") in ("water", "dispatch_water"):
                self.water_by_site[self.site_of.get(entry.get("worker_id"), "?")] += 1

    # ------------------------------------------------------------- results
    def share(self, cell):
        return cell[1] / cell[0] if cell[0] else None

    def recommendations(self):
        recs = []
        hours_seen = sorted({h for (_, h), c in self.cells.items() if c[0] >= MIN_MINUTES_PER_CELL})
        minutes_so_far = int((self.last_sim - self.first_sim).total_seconds() // 60) if self.first_sim else 0

        # 1) Scheduling: the site and hour where risk is highest vs. its calmest hour.
        if len(hours_seen) >= 2:
            best = None
            for site in {s for s, _ in self.cells}:
                shares = {h: self.share(self.cells[(site, h)]) for h in hours_seen if self.cells[(site, h)][0] >= MIN_MINUTES_PER_CELL}
                if len(shares) < 2:
                    continue
                peak_h = max(shares, key=shares.get)
                low_h = min(shares, key=shares.get)
                if best is None or shares[peak_h] > best[1]:
                    best = (site, shares[peak_h], peak_h, shares[low_h], low_h)
            if best and best[1] > 0:
                site, peak, peak_h, low, low_h = best
                job = max((j for s, j in self.jobs if s == site),
                          key=lambda j: self.share(self.jobs[(site, j)]) or 0, default="heavy")
                ratio = f"{peak / low:.1f}× higher" if low > 0 else "far higher"
                recs.append({"type": "Scheduling", "text":
                    f"Move heavy {job} tasks in {site} away from {peak_h:02d}:00: {peak:.0%} of worker-time there was at risk, "
                    f"{ratio} than at {low_h:02d}:00 ({low:.0%})."})
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
                f"{runs} water run{'s' if runs != 1 else ''} went there this shift."})
        elif self.water_by_site:
            site, runs = self.water_by_site.most_common(1)[0]
            recs.append({"type": "Resources", "text":
                f"{site} needed the most water runs ({runs}): keep a water station there."})

        # 3) Policy: how much risk falls outside the fixed midday ban.
        if self.risk_total:
            outside = 1 - self.risk_in_ban / self.risk_total
            recs.append({"type": "Policy", "text":
                f"Use WBGT-based break rules alongside the fixed 12:30–15:00 midday work ban: "
                f"{outside:.0%} of at-risk worker-time this shift fell outside the ban hours."})

        # 4) People: the job type that spends most time at risk.
        jobs = self.job_shares()
        if jobs:
            avg = sum(j["at_risk"] for j in jobs) / max(1, sum(j["minutes"] for j in jobs))
            top = jobs[0]
            if avg > 0 and top["share"] > avg * 1.2:
                recs.append({"type": "Crews", "text":
                    f"Give {top['job']} crews extra shade breaks: {top['share']:.0%} of their time was at risk, "
                    f"{top['share'] / avg:.1f}× the crew average."})
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
        predicted = len(self.predicted_ids)
        caught = predicted - len(self.escalated_ids & self.predicted_ids)
        return {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
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
            "kpis": {
                "early_warnings": predicted,
                "median_warning_minutes": round(statistics.median(self.warning_minutes)) if self.warning_minutes else None,
                "interventions": self.interventions,
                "caught_early_pct": round(100 * caught / predicted) if predicted else None,
                "danger_alerts": self.red_alerts,
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


def _join(items):
    items = list(items)
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]
