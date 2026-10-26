"""Coach feedback on each new ride, pushed to the phone right after the sync that brought it in.

The facts (zones, new bests, planned vs actual per work segment, the week so far, form, Apple Health) are computed
here; the model only judges and writes, so a small model is enough. One call per ride, ~5k tokens in.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import date, datetime, timedelta, timezone
from statistics import StatisticsError, correlation, median

from . import coach, coaches, ha, health, llm, memory, metrics, overview, sports, stats, workouts
from .coach import HEALTH_RULES, counts_for
from .config import HEALTH_SOURCES, Config
from .db import DB

log = logging.getLogger(__name__)

WINDOW = timedelta(hours=48)       # only rides this recent get a notification, so a backfill never floods the phone
STREAM_WAIT = timedelta(hours=6)   # wait this long for power data before giving feedback without it
PER_RUN = 3                        # feedback calls per sync, at most
BEST_DURATIONS = {5: "5 s", 60: "1 min", 300: "5 min", 1200: "20 min", 3600: "60 min"}
MAX_LAG_S = 600       # a late start of the Zwift workout we still find
MIN_CORRELATION = 0.6  # below this the ride doesn't follow the plan closely enough to judge reps
ON_TARGET = 0.03

SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string", "description": "The verdict in under 50 characters; the notification title"},
        "notification": {"type": "string", "description": "2-4 short sentences, under 300 characters, plain text"},
        "detail": {"type": "string", "description": "Markdown, 80-200 words, for the ride page"},
    },
    "required": ["headline", "notification", "detail"],
    "additionalProperties": False,
}

SYSTEM = """You are {coach}, coaching one amateur road and gravel cyclist. They just finished a ride and you \
give feedback on it. You get a JSON with the ride, the session you planned for that day (if any), the work \
segments of a planned Zwift workout with target and actual watts, the week so far, their form, their goals and, \
when present, readiness data from Apple Health or Garmin (health, health_source).

Write three things:
- headline: the verdict in under 50 characters, e.g. "Sweet spot nailed, last rep faded". It's the title of \
a phone notification. No emoji, no exclamation marks.
- notification: 2-4 short sentences, under 300 characters, plain text, read on a lock screen. How the ride went \
against the plan (or its purpose if it wasn't planned), one specific number that backs it up, and what to do \
next (recovery, food, tomorrow's session).
- detail: Markdown, 80-200 words, for the ride page. Execution against the plan (per segment when given), what \
the numbers say (zones, decoupling, new bests), readiness when health data says something, and what comes next. \
No headings, short paragraphs or a short list.

Rules:
- Use only the numbers in the JSON. Don't invent splits, feelings or conditions you weren't given.
- segments are the planned work segments with target and actual watts, already aligned with the ride. If \
alignment is "unclear", the ride didn't follow the workout closely enough: don't judge reps, judge the ride.
- A segment within 3% of target is on target. Over target on a hard rep is not praise: the plan was the plan.
- ftp_mismatch means every segment is off target by the same amount: Zwift's FTP setting differs from the FTP \
here. That's a settings issue, not execution. Say so and suggest setting Zwift's FTP to ftp_used_w.
- A test (planned_session.test_protocol not "none"): judge pacing and the result, never call the final step a miss.
- HR-only or estimated rides: say less about intensity, the data is thin.
- An unplanned ride on a rest day: say what it does to the week, without guilt-tripping, unless it puts the next \
hard session at risk.
- decoupling_pct is only given for steady rides: under 5% is good aerobic durability; indoors, heat inflates it.
- new_bests beat the best of the 90 days before this ride. Mention them; they're motivating. history_thin means \
there's too little earlier power data to call anything a best.
- form: tsb_before is form going into the ride, tsb_tomorrow after it.
- The planner reads health with this rule. Apply the same rule when you talk about what comes next, and don't \
change the next session beyond what it says:
{health_rules}
- planned_session.planned_by isn't you when the athlete switched to you this week: judge the ride against it as \
written and don't critique its design here.
- athlete_history is background: who coached them before you, earlier injuries and events (past_notes) and \
lasting facts (memory). Use it when it explains this ride, e.g. an old injury or a known preference; don't recite it.
- English, in your own coaching voice:

{philosophy}"""


RUN_SYSTEM = """You are {coach}, coaching one amateur runner (who may also ride: see the week). They just finished a run \
and you give feedback on it. You get a JSON with the run (pace, HR, HR drift between its halves), the session you \
planned for that day (if any) with its target pace, the week so far across both sports, their form, their goals and, \
when present, readiness data (health, health_source).

Write three things:
- headline: the verdict in under 50 characters, e.g. "Easy run kept easy, HR steady". The title of a phone \
notification. No emoji, no exclamation marks.
- notification: 2-4 short sentences, under 300 characters, plain text, read on a lock screen. How the run went \
against the plan (or its purpose if it wasn't planned), one specific number that backs it up, and what to do next.
- detail: Markdown, 80-200 words, for the activity page. Pace against target_pace, HR against threshold_hr_bpm \
(easy runs under ~85%), HR drift (under ~5% on a steady run is good durability; heat, hills and a fast finish raise \
it), and what comes next. No headings, short paragraphs or a short list.

Rules:
- Use only the numbers in the JSON. Average pace on a hilly or trail run reads slow: judge effort from HR there.
- An easy run done too fast is the thing to point out, kindly: it costs the next quality session.
- run_load: this week's run minutes against the run durability cap. Near or over it, say to keep the next runs easy \
and short: impact load builds slower than fitness, whatever the bike says.
- form: tsb_before is form going into the run, tsb_tomorrow after it.
- The planner reads health with this rule; apply the same rule for what comes next:
{health_rules}
- athlete_history is background; use it only when it explains this run.
- English, in your own coaching voice:

{philosophy}"""


def plan_day(db: DB, day: date, name: str = "", a: dict | None = None) -> tuple[dict | None, dict | None]:
    """(active plan of that week, the session this activity was). A session from another day of the week whose title
    is in the activity's name wins (Zwift names the ride after the workout, and the athlete swaps days); else that
    day's. With `a`, only sessions it can be (a ride for a ride, a run for a run)."""
    plan = db.active_plan(stats.week_start(day).isoformat())
    sessions = [d for d in (plan or {}).get("payload", {}).get("days", []) if d["kind"] != "rest"
                and (a is None or counts_for(d, a))]
    named = [d for d in sessions if overview._norm(d["title"]) and overview._norm(d["title"]) in overview._norm(name)]
    if named:
        return plan, min(named, key=lambda d: abs((date.fromisoformat(d["date"]) - day).days))
    return plan, next((d for d in sessions if d["date"] == day.isoformat()), None)


def _target_series(steps: list[dict], per: float, n: int) -> list[float | None]:
    """Planned %FTP at the middle of each sample block, None past the workout's end."""
    segs, out, k = workouts.segments(steps), [], 0
    for i in range(n):
        t = (i + 0.5) * per
        while k < len(segs) and t >= segs[k][0] + segs[k][1]:
            k += 1
        if k == len(segs):
            out.append(None)
            continue
        s0, d, lo, hi = segs[k]
        out.append(lo + (hi - lo) * (t - s0) / d)
    return out


def _lag(watts: list[float], target: list[float | None], per: float) -> tuple[int, float, float]:
    """(best shift in blocks, its correlation, the correlation at no shift) of ride power against the plan."""
    best, best_r, r0 = 0, -1.0, -1.0
    for lag in range(0, int(MAX_LAG_S / per) + 1):
        pairs = [(watts[i + lag], t) for i, t in enumerate(target) if t is not None and i + lag < len(watts)]
        try:
            r = correlation([p for p, _ in pairs], [t for _, t in pairs]) if len(pairs) >= 10 else -1.0
        except StatisticsError:  # a constant series
            r = -1.0
        r0 = r if lag == 0 else r0
        if r > best_r + 1e-9:
            best, best_r = lag, r
    return best, best_r, r0


def segments_vs_actual(a: dict, steps: list[dict]) -> dict:
    """Target and actual watts for each work segment (≥ 88% FTP, at least 30 s) of the planned workout.

    The plan is lined up with the ride first (a late start of the workout shifts everything). Only sample blocks
    fully inside a segment count, minus the first (ERG settling), so a recovery never leaks into a rep. Segments
    with fewer than two blocks left are not judged: at ~15 s blocks that's reps under ~45 s. A uniform offset over all
    segments is reported as an FTP mismatch with Zwift, not as missed reps."""
    watts, ftp = a.get("spark_watts") or [], a.get("ftp_used") or 0
    elapsed = a.get("elapsed_time") or a.get("moving_time") or 0
    if len(watts) < 10 or not ftp or not elapsed or not steps:
        return {"segments": []}
    per = elapsed / len(watts)
    lag, r, r0 = _lag(watts, _target_series(steps, per, len(watts)), per)
    if r < MIN_CORRELATION:
        return {"segments": [], "alignment": "unclear"}
    if r <= r0 + 0.05:
        lag = 0  # not clearly better than taking the ride as it started
    work = [(s, d, (lo + hi) / 2) for s, d, lo, hi in workouts.segments(steps) if (lo + hi) / 2 >= 0.88 and d >= 30]
    out = []
    for i, (start, dur, p) in enumerate(work[:20], 1):
        lo, hi = math.ceil(start / per) + lag + 1, math.floor((start + dur) / per) + lag  # +1: ERG settling
        sel = watts[lo:hi]
        if hi - lo < 2 or len(sel) < hi - lo:
            continue  # too short to judge, or the ride ended first
        out.append({"segment": f"{i}/{len(work)}", "minute": round(start / 60, 1), "seconds": dur,
                    "target_w": round(p * ftp), "actual_w": round(sum(sel) / len(sel))})
    res: dict = {"segments": out, "alignment": "shifted" if lag else "ok"}
    if lag:
        res["aligned_offset_s"] = round(lag * per)
    ratios = [s["actual_w"] / s["target_w"] for s in out]
    if len(ratios) >= 3:
        m = median(ratios)
        if abs(m - 1) > ON_TARGET and all(abs(x - m) <= ON_TARGET for x in ratios):
            res["ftp_mismatch"] = {"zwift_ftp_estimate": round(ftp * m), "offset_pct": round((m - 1) * 100, 1)}
    return res


def new_bests(a: dict, acts: list[dict]) -> list[dict] | None:
    """Durations where this ride beat the 90 days before it by at least 1%. None when fewer than 5 earlier rides
    have power: then everything would look like a best."""
    day = a["start_date_local"][:10]
    lo = (date.fromisoformat(day) - timedelta(days=90)).isoformat()
    earlier = [x["mmp"] for x in acts if x.get("mmp") and x["id"] != a["id"]
               and lo <= x["start_date_local"] < a["start_date_local"]]
    if len(earlier) < 5:
        return None
    prior, mmp = metrics.best_curve(earlier), a.get("mmp") or {}
    return [{"duration": label, "watts": round(mmp[s]), "previous_best_90d": round(prior[s])}
            for s, label in BEST_DURATIONS.items() if mmp.get(s) and s in prior and mmp[s] >= prior[s] * 1.01]


def hr_drift(a: dict, planned: dict | None) -> float | None:
    """Second half's mean HR over the first half's, in %, without the first 10 min (warm-up): the cardiac drift of a
    steady run. Only for runs of 40 min or more that weren't planned as hard (reps make any drift meaningless). HR, not
    pace:HR decoupling: runs have no speed stream here."""
    secs = a.get("moving_time") or 0
    if secs < 40 * 60 or (planned and planned.get("intensity") in coach.RUN_HARD | {"tempo"}):
        return None
    hr = a.get("spark_hr") or []
    hr = [h for h in hr[round(len(hr) * 600 / secs):] if h]
    if len(hr) < 20:
        return None
    half = len(hr) // 2
    first, second = sum(hr[:half]) / half, sum(hr[half:]) / (len(hr) - half)
    return round((second - first) / first * 100, 1)


def run_context(db: DB, cfg: Config, a: dict) -> tuple[dict, coaches.Coach]:
    """The run branch: pace against the plan's target pace, HR against threshold HR, HR drift, and this week's run
    minutes against the durability cap."""
    day = date.fromisoformat(a["start_date_local"][:10])
    acts = [x for x in db.activities(since_local=(day - timedelta(days=400)).isoformat())
            if x["start_date_local"] <= a["start_date_local"]]
    plan, planned = plan_day(db, day, a["name"] or "", a)
    at = datetime.fromisoformat(a["start_date_local"][:19])
    c = memory.coach_in_charge(db, cfg, at, "running")
    d = stats.dashboard(acts, day, cfg)
    running = stats.running(acts, day, cfg)
    week_runs = sum((x["moving_time"] or 0) for x in sports.runs(acts) if x["start_date_local"][:10] >= stats.week_start(day).isoformat()) / 60
    ctx = {
        "run": {"date": day.isoformat(), "weekday": day.strftime("%A"), "name": a["name"], "type": a["sport_type"],
                "treadmill": bool(a["trainer"]), "minutes": round((a["moving_time"] or 0) / 60),
                "km": round((a["distance"] or 0) / 1000, 2), "pace": stats.pace(a), "elev_m": round(a["elevation_gain"] or 0),
                "avg_hr": a["avg_hr"], "max_hr": a["max_hr"], "threshold_hr_bpm": cfg.run_threshold_hr,
                "hr_drift_pct": hr_drift(a, planned), "tss": a["tss"], "tss_source": a["tss_source"]},
        "planned_session": {k: planned.get(k) for k in ("date", "kind", "title", "description", "duration_min",
                                                         "intensity", "target_pace", "surface")}
        | {"planned_by": coaches.name_of(planned.get("coach"))} if planned else None,
        "threshold_pace": running["threshold_pace"],
        "run_load": {"minutes_this_week": round(week_runs), "cap_minutes": running["cap_minutes"],
                     "longest_run_cap_min": running["longest_run_cap_min"]},
        "week_so_far": [{"date": p["date"], "sport": sports.day_sport(p), "planned": p["title"] if p["kind"] != "rest" else "Rest",
                         "status": st} for p, st in overview.day_statuses(plan, acts, day.isoformat())] if plan else [],
        "form": {"ctl": d["ctl"], "atl": d["atl"], "tsb_before": d["tsb"], "tsb_tomorrow": round(d["ctl"] - d["atl"], 1)},
        "athlete": {"goals": cfg.goals.strip() or "Not stated"},
        "coach_notes": [{k: n[k] for k in ("kind", "start", "end", "text")} for n in db.active_notes(day.isoformat())],
    }
    h = memory.history(db, cfg, at)
    ctx["athlete_history"] = {k: h[k] for k in ("coaches", "past_notes", "memory")}
    if health.enabled(cfg):
        ctx["health"], ctx["health_source"] = health.summary(db, day), HEALTH_SOURCES[cfg.health_source]
    return ctx, c


def context(db: DB, cfg: Config, a: dict) -> tuple[dict, coaches.Coach]:
    if sports.kind(a["sport_type"]) == "run":
        return run_context(db, cfg, a)
    day = date.fromisoformat(a["start_date_local"][:10])
    # Nothing after this ride: asking again later must not see the rest of the week
    acts = [x for x in db.activities(since_local=(day - timedelta(days=400)).isoformat())
            if x["start_date_local"] <= a["start_date_local"]]
    plan, planned = plan_day(db, day, a["name"] or "", a)
    at = datetime.fromisoformat(a["start_date_local"][:19])
    c = memory.coach_in_charge(db, cfg, at, "cycling")
    d = stats.dashboard(acts, day, cfg)
    zones = metrics.zone_seconds(a["power_hist"], a["ftp_used"]) if a.get("power_hist") and a.get("ftp_used") else []
    ride = {
        "date": day.isoformat(), "weekday": day.strftime("%A"), "start": a["start_date_local"][11:16], "name": a["name"],
        "type": a["sport_type"], "indoor": bool(a["trainer"] or a["sport_type"] == "VirtualRide"),
        "minutes": round((a["moving_time"] or 0) / 60), "km": round((a["distance"] or 0) / 1000, 1),
        "elev_m": round(a["elevation_gain"] or 0), "avg_w": a["avg_watts"], "np_w": round(a["np"]) if a.get("np") else None,
        "ftp_used_w": a["ftp_used"], "if": a["intensity"], "tss": a["tss"], "tss_source": a["tss_source"],
        "avg_hr": a["avg_hr"], "max_hr": a["max_hr"],
        "zone_minutes": {z: round(s / 60) for (z, _), s in zip(metrics.ZONES, zones)} if zones else None,
        "maximal_effort": stats.maximal_effort(a),
    }
    seg = (segments_vs_actual(a, planned.get("steps") or [])
           if planned and planned["kind"] == "zwift" and planned.get("test_protocol", "none") == "none" else {"segments": []})
    steady = (a.get("np") and a.get("avg_watts") and a["np"] / a["avg_watts"] <= 1.05 and (a["moving_time"] or 0) >= 3600
              and not seg["segments"])
    if steady and a.get("decoupling") is not None:
        ride["decoupling_pct"] = a["decoupling"]
    bests = new_bests(a, acts)
    week = []
    if plan:
        week = [{"date": p["date"], "planned": p["title"] if p["kind"] != "rest" else "Rest", "status": status}
                for p, status in overview.day_statuses(plan, acts, day.isoformat())]
        upcoming = [p for p in plan["payload"]["days"] if p["date"] > day.isoformat()]
        nxt = next((p for p in upcoming if p["kind"] != "rest"), None)
    else:
        upcoming, nxt = [], None
    ctx = {
        "ride": ride,
        "planned_session": {k: planned.get(k, "none" if k == "test_protocol" else None) for k in (
            "date", "kind", "title", "description", "duration_min", "intensity", "target_tss", "test_protocol")}
        | {"planned_by": coaches.name_of(plan["coach"])} if planned else None,
        **seg,
        **({"new_bests": bests} if bests is not None else {"history_thin": True}),
        "week_so_far": {"week_focus": plan["payload"]["week_focus"] if plan else None, "days": week,
                        "tss_done": d["this_week"]["tss"],
                        "tss_planned_to_date": sum(p["target_tss"] for p in plan["payload"]["days"]
                                                   if p["date"] <= day.isoformat()) if plan else None,
                        "tss_planned_week": sum(p["target_tss"] for p in plan["payload"]["days"]) if plan else None},
        "tomorrow": next(({"date": p["date"], "title": p["title"], "kind": p["kind"], "intensity": p["intensity"]}
                          for p in upcoming if p["date"] == (day + timedelta(days=1)).isoformat()), None),
        "next_session": {"date": nxt["date"], "title": nxt["title"], "intensity": nxt["intensity"]} if nxt else None,
        # The fitness series gives form going into a day; after the ride it's today's CTL - ATL
        "form": {"ctl": d["ctl"], "atl": d["atl"], "tsb_before": d["tsb"], "tsb_tomorrow": round(d["ctl"] - d["atl"], 1)},
        "athlete": {"goals": cfg.goals.strip() or "Not stated", "level": cfg.athlete_level, "ftp_w": d["ftp"],
                    "w_per_kg": d["wkg"]},
        "coach_notes": [{k: n[k] for k in ("kind", "start", "end", "text")} for n in db.active_notes(day.isoformat())],
    }
    h = memory.history(db, cfg, at)
    ctx["athlete_history"] = {k: h[k] for k in ("coaches", "past_notes", "memory")}
    if health.enabled(cfg):
        ctx["health"], ctx["health_source"] = health.summary(db, day), HEALTH_SOURCES[cfg.health_source]
    return ctx, c


async def generate(db: DB, cfg: Config, a: dict) -> dict:
    """Asks the coach about one ride and stores the feedback on it."""
    ctx, c = context(db, cfg, a)
    system = RUN_SYSTEM if sports.kind(a["sport_type"]) == "run" else SYSTEM
    msg = await llm.run(cfg.anthropic_api_key, system.format(coach=c.name, philosophy=c.philosophy, health_rules=HEALTH_RULES),
                        [{"role": "user", "content": f"```json\n{json.dumps(ctx, indent=1)}\n```"}],
                        schema=SCHEMA, effort="medium", max_tokens=8000, db=db, purpose="ride")
    fb = json.loads(llm.text_of(msg)) | {"coach": c.key, "model": msg.model,
                                         "at": datetime.now().isoformat(timespec="seconds")}
    db.update_activity(a["id"], feedback=fb)
    return fb


def stats_line(a: dict) -> str:
    parts = [f"{round((a['moving_time'] or 0) / 60)} min", f"{(a['distance'] or 0) / 1000:.1f} km"]
    if not sports.is_ride(a) and stats.pace(a):
        parts.append(stats.pace(a))
    if a.get("tss"):
        parts.append(f"TSS {round(a['tss'])}")
    if a.get("np"):
        parts.append(f"NP {round(a['np'])} W")
    elif a.get("avg_hr"):
        parts.append(f"HR {round(a['avg_hr'])}")
    return " · ".join(parts)


async def after_sync(db: DB, cfg: Config, now: datetime | None = None) -> int:
    """Feedback and a notification for each new ride. Runs whenever Claude is set up: it's tied to a ride you just
    did, not to a schedule. Returns how many rides were handled."""
    now = now or datetime.now(timezone.utc)
    done = 0
    for a in db.activities_to_notify((now - WINDOW).strftime("%Y-%m-%dT%H:%M:%SZ")):
        if sports.kind(a["sport_type"]) == "walk":  # walks count for load, silently: no push for every dog walk
            db.update_activity(a["id"], notified=1)
            continue
        started = datetime.fromisoformat(a["start_date"].replace("Z", "+00:00"))
        if not a["streams_fetched"] and now - started < STREAM_WAIT:
            continue  # power data comes on a later sync; feedback without it would be thin
        if done >= PER_RUN:
            break
        fb = a.get("feedback")
        if cfg.anthropic_api_key and not fb:
            try:
                fb = await generate(db, cfg, a)
            except Exception as e:  # noqa: BLE001 - the notification still goes out, without feedback
                log.warning("Ride feedback for %s failed: %s", a["id"], e)
        title = fb["headline"] if fb else f"Synced: {a['name']}"
        body = f"{stats_line(a)}\n{fb['notification']}" if fb else stats_line(a)
        await ha.notify(cfg.notify_service, title, body, ha.tap(f"/activity/{a['id']}", "rides"))
        db.update_activity(a["id"], notified=1)
        done += 1
    return done
