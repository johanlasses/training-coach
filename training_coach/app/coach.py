"""Weekly plan generation: snapshot → Claude (structured output) → validation → stored plan."""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta

from . import llm, workouts
from .config import Config
from .db import DB

log = logging.getLogger(__name__)

HARD = {"threshold", "vo2max", "anaerobic"}
INTENSITIES = ["recovery", "endurance", "tempo", "sweetspot", "threshold", "vo2max", "anaerobic", "mixed"]

STEP_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": ["warmup", "cooldown", "steady", "intervals", "freeride"]},
        "duration_s": {"type": "integer", "description": "Step length; 0 for intervals"},
        "power_low": {"type": "number", "description": "Start power as fraction of FTP (e.g. 0.55); 0 for intervals"},
        "power_high": {"type": "number", "description": "End power as fraction of FTP; equal to power_low for steady"},
        "repeat": {"type": "integer", "description": "Interval repeats; 0 unless intervals"},
        "on_s": {"type": "integer"},
        "off_s": {"type": "integer"},
        "on_power": {"type": "number"},
        "off_power": {"type": "number"},
        "cadence": {"type": "integer", "description": "Target rpm, 0 for free cadence"},
        "text": {"type": "string", "description": "Short cue shown in Zwift, may be empty"},
    },
    "required": ["type", "duration_s", "power_low", "power_high", "repeat", "on_s", "off_s",
                 "on_power", "off_power", "cadence", "text"],
    "additionalProperties": False,
}

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "week_focus": {"type": "string"},
        "rationale": {"type": "string", "description": "Why this week looks like this, 3-6 sentences"},
        "days": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "YYYY-MM-DD"},
                    "kind": {"type": "string", "enum": ["rest", "zwift", "outdoor"]},
                    "title": {"type": "string"},
                    "description": {"type": "string", "description": "Purpose and execution cues"},
                    "duration_min": {"type": "integer"},
                    "intensity": {"type": "string", "enum": INTENSITIES},
                    "target_tss": {"type": "integer"},
                    "bike": {"type": "string", "enum": ["none", "road", "gravel", "trainer"]},
                    "terrain": {"type": "string", "description": "Outdoor only: route/terrain hint, else empty"},
                    "steps": {"type": "array", "items": STEP_SCHEMA,
                              "description": "Zwift only: full workout; empty for rest/outdoor"},
                },
                "required": ["date", "kind", "title", "description", "duration_min", "intensity",
                             "target_tss", "bike", "terrain", "steps"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["week_focus", "rationale", "days"],
    "additionalProperties": False,
}

SYSTEM = """You are an experienced cycling coach for a single amateur road and gravel cyclist in \
Mälardalen, Sweden. They train on Zwift indoors and ride road/gravel outside. Their current goal \
is general fitness and FTP gain, organised as rolling 4-week blocks (3 build weeks + 1 recovery week).

You receive a JSON snapshot of their training (load metrics, power curve, recent rides, season and \
daylight, availability) and plan the requested days.

Principles:
- Base intensities on the given FTP. Express all Zwift power as fractions of FTP.
- Pyramidal/polarised distribution: most time in Z1–Z2, 1–2 quality sessions per build week \
(sweetspot, threshold or VO2max), never two hard days in a row.
- Respect form: if TSB is below -25, reduce load; if above +10, the athlete can absorb more.
- Keep CTL ramp at roughly +3 to +6 per week in build weeks. Recovery weeks ~60-70% of a build week's TSS, no VO2max.
- Each day's duration must fit within the availability minutes for that weekday (0 = rest day).
- Choose outdoor rides from the season and daylight: weekend outdoor rides when the season allows, weekday \
outdoor rides only with daylight after work (sunset well after 18:00). Otherwise use Zwift. The athlete swaps \
an outdoor ride for Zwift themselves if the weather turns, so give outdoor days a Zwift-friendly purpose.
- Outdoor rides: endurance/tempo, group-ride or long gravel days. Say road or gravel, give a terrain hint.
- Zwift workouts: complete step lists with warmup and cooldown; step durations must add up to \
duration_min. Use cadence targets sparingly and purposefully.
- target_tss: your estimate for the session (rest = 0).
- Write titles and descriptions in English, concise and specific — like a coach's note, not marketing."""


def block_week(db: DB, week_start: date) -> int:
    start = db.get("block_start")
    if not start:
        db.set("block_start", week_start.isoformat())
        return 1
    weeks = (week_start - date.fromisoformat(start)).days // 7
    return weeks % 4 + 1


def validate(plan: dict, dates: list[str], cfg: Config, ctl: float, done_tss: float, recovery: bool) -> list[str]:
    """Hard checks the plan must pass. Also normalises Zwift TSS to the profile estimate (in place)."""
    errors = []
    days = plan["days"]
    if [d["date"] for d in days] != dates:
        errors.append(f"days must be exactly these dates in order: {dates}")
        return errors
    for d in days:
        wd = date.fromisoformat(d["date"]).weekday()
        limit = cfg.max_minutes(wd)
        if d["kind"] != "rest" and d["duration_min"] > limit:
            errors.append(f"{d['date']}: {d['duration_min']} min exceeds availability {limit} min")
        if d["kind"] == "zwift":
            if not d["steps"]:
                errors.append(f"{d['date']}: Zwift session has no steps")
                continue
            secs = workouts.total_seconds(d["steps"])
            if abs(secs / 60 - d["duration_min"]) > max(5, d["duration_min"] * 0.1):
                errors.append(f"{d['date']}: steps sum to {secs // 60} min but duration_min is {d['duration_min']}")
            for s in d["steps"]:
                powers = [s["on_power"], s["off_power"]] if s["type"] == "intervals" else (
                    [] if s["type"] == "freeride" else [s["power_low"], s["power_high"]])
                if any(not 0.3 <= p <= 2.5 for p in powers):
                    errors.append(f"{d['date']}: step power {powers} outside 0.3–2.5 × FTP")
                    break
                if s["type"] == "intervals" and (s["repeat"] < 1 or s["on_s"] < 5):
                    errors.append(f"{d['date']}: intervals need repeat ≥ 1 and on_s ≥ 5")
                    break
            d["target_tss"] = round(workouts.estimated_tss(d["steps"]))
        elif d["steps"]:
            d["steps"] = []
        if d["kind"] == "rest":
            d["target_tss"] = 0
    for a, b in zip(days, days[1:]):
        if a["intensity"] in HARD and b["intensity"] in HARD and a["kind"] != "rest" and b["kind"] != "rest":
            errors.append(f"{a['date']} and {b['date']}: two hard days in a row")
    week_tss = done_tss + sum(d["target_tss"] for d in days)
    cap = max(250, 7 * ctl * 0.75) if recovery else max(350, 7 * ctl + 280)
    if week_tss > cap:
        errors.append(f"week TSS {week_tss:.0f} (incl. {done_tss:.0f} already done) exceeds cap {cap:.0f}"
                      + (" for a recovery week" if recovery else ""))
    return errors


async def generate_plan(db: DB, cfg: Config, snapshot: dict, week_start: date, today: date) -> int:
    """Plan from max(today, week_start) through Sunday. Returns the stored plan id."""
    first = max(today, week_start)
    dates = [(first + timedelta(days=i)).isoformat() for i in range((week_start + timedelta(days=6) - first).days + 1)]
    bw = block_week(db, week_start)
    recovery = bw == 4
    done = [r for r in snapshot["recent_rides"] if week_start.isoformat() <= r["date"] < first.isoformat()]
    done_tss = sum(r["tss"] or 0 for r in done)

    request = {
        "plan_dates": dates,
        "block_week": bw,
        "block_week_type": "recovery" if recovery else "build",
        "already_done_this_week": done,
        "snapshot": snapshot,
    }
    messages = [{"role": "user", "content": (
        f"Plan these days: {', '.join(dates)}. This is week {bw} of the 4-week block "
        f"({'recovery' if recovery else 'build'} week).\n\n```json\n{json.dumps(request, indent=1)}\n```")}]

    warnings: list[str] = []
    plan = None
    for attempt in range(2):
        msg = await llm.run(cfg.anthropic_api_key, SYSTEM, messages, schema=PLAN_SCHEMA, effort="high")
        plan = json.loads(llm.text_of(msg))
        warnings = validate(plan, dates, cfg, snapshot["load"]["ctl"], done_tss, recovery)
        if not warnings:
            break
        log.info("Plan attempt %d failed validation: %s", attempt + 1, warnings)
        messages += [
            {"role": "assistant", "content": msg.content},
            {"role": "user", "content": "The plan failed these checks; return a corrected full plan:\n- "
                                        + "\n- ".join(warnings)},
        ]
    plan["block_week"] = bw
    return db.save_plan(week_start.isoformat(), datetime.now().isoformat(timespec="seconds"),
                        llm.MODEL, plan, warnings)


def compliance(plan_day: dict, acts_that_day: list[dict]) -> str:
    """done | partial | missed | extra | rest | upcoming."""
    if plan_day["kind"] == "rest":
        return "extra" if acts_that_day else "rest"
    if not acts_that_day:
        return "missed"
    minutes = sum(a["moving_time"] or 0 for a in acts_that_day) / 60
    return "done" if minutes >= 0.75 * plan_day["duration_min"] else "partial"
