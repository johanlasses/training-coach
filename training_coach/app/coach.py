"""Weekly plan generation: snapshot → Claude (structured output) → validation → stored plan."""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta

from . import coaches, ftptest, llm, memory, workouts
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
        "text": {"type": "string", "description": "Short cue shown in Zwift at step start, may be empty"},
        "cues": {
            "type": "array",
            "description": "More on-screen messages during the step, in the coach's voice",
            "items": {
                "type": "object",
                "properties": {
                    "offset_s": {"type": "integer", "description": "Seconds from step start (intervals: from block start)"},
                    "text": {"type": "string"},
                },
                "required": ["offset_s", "text"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["type", "duration_s", "power_low", "power_high", "repeat", "on_s", "off_s",
                 "on_power", "off_power", "cadence", "text", "cues"],
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
                    "test_protocol": {"type": "string", "enum": ["none", *ftptest.PROTOCOLS],
                                      "description": "The FTP test this session is; none for training sessions"},
                },
                "required": ["date", "kind", "title", "description", "duration_min", "intensity",
                             "target_tss", "bike", "terrain", "steps", "test_protocol"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["week_focus", "rationale", "days"],
    "additionalProperties": False,
}

HEALTH_RULES = """- snapshot.health, when present, holds Apple Health trends: the last 7 days against a baseline of the 28 days \
before them, with the days of data in each. Read them as a readiness signal, never from a single day, and only \
when the 7 days hold at least 5 days of data and the baseline at least 14. Signals: HRV change_pct about -10% \
or lower, resting HR change +4 bpm or more, 7-day sleep under ~6.5 h. Two signals together: swap the next hard \
session for endurance or rest and say why. One signal: keep the session but shorten it or cap it at the low \
end of its targets, and mention it. A sharp HRV rise during heavy load isn't freshness. Weight is only for \
W/kg, and Apple's VO2 max comes from walking and running, so neither is a readiness signal. Missing data means \
nothing."""

SYSTEM = """You are a cycling coach for a single amateur road and gravel cyclist in Mälardalen, Sweden. \
They train on Zwift indoors and ride road/gravel outside. Training is organised as rolling 4-week blocks \
(3 build weeks + 1 recovery week).

You receive a JSON snapshot of their training (load metrics, power curve, recent rides, season and \
daylight, availability) and plan the requested days. snapshot.athlete holds their own goals, self-rated \
level and the number of sessions per week they aim for. Plan toward those goals. Treat sessions per week as \
a target, not a rule: recovery weeks or high fatigue may justify fewer.

Hard rules, whichever coaching style you follow:
- Base intensities on the given FTP. Express all Zwift power as fractions of FTP.
- Respect form: if TSB is below -25, reduce load; if above +10, the athlete can absorb more.
- Keep CTL ramp at roughly +3 to +6 per week in build weeks. Recovery weeks ~60-70% of a build week's TSS, \
at most one hard session and no VO2max/anaerobic. Openers (under 5 min in total above 105% FTP) are labelled endurance.
- A day counts as hard if it has ≥10 min at ≥88% FTP or ≥5 min above 105% FTP (whatever you label it), \
an intensity of threshold/VO2max/anaerobic, or is an outdoor "mixed" ride. Rides already done count too \
(IF ≥ 0.85 or maximal_effort is hard), including yesterday's.
- Each day's duration must fit within the availability minutes for that weekday (0 = rest day).
- snapshot.coach_notes are things the athlete told you in chat. Absence dates are rest days. Before an event \
note, shape the days so they arrive fresh (an easy day or openers the day before, no hard session in the last \
2 days), plan the event day itself as the ride (it may run past the availability limit), and make the day after \
rest or easy. For the main goal event, taper over 7-10 days: volume down ~40-60%, keep short intensity and the \
same number of rides. Injury notes override the progression: back off until they end. Illness: no training \
with fever or symptoms below the neck; after the note ends, at least 2-3 easy days before any hard session, \
and after a fever roughly as many easy days as days sick. A note asking for a test counts as the athlete asking.
{health_rules}
- snapshot.athlete_history is the longer story: coaches (who coached them, from when, for how many planned \
weeks; the run with "you": true is you, and a later run means you are taking over), changes (profile edits such as \
goals, FTP override, weight, availability and coach switches, with dates), past_notes (earlier injuries, illnesses, \
absences and events, with the day they really ended; cancelled ones never happened), ftp_results and memory \
(lasting facts you or earlier coaches learned in chat).
- Taking over from another coach (their run ended in the last few weeks): build on that training instead of starting \
over. In your first week keep weekly TSS within about ±10% of the last 2-3 weeks' average, bring in your own \
structure over 1-2 weeks with at most one more hard session than they planned, and mention the handover in \
week_focus. If they switched because they felt worn down or stale (chat, memory), or TSB has been below -20 for a \
week or more, make the first week a hold week: same or lower TSS and fewer hard sessions than before. After an FTP \
change (ftp_results, changes), judge continuity by hours and kJ as well as TSS, since TSS moves with the FTP.
- An illness note that ended in the last 3 days (past_notes) still counts for the illness rule. An injury that ended \
under ~6 weeks ago: bring back the load that caused it (long climbs, low-cadence torque, long efforts in the drops) \
over 2-3 weeks, the longest ride growing ~10-20% a week. Older healed injuries are background unless a note or \
memory says it came back.
- Weigh memory, don't obey it. If a session type they dislike is still needed for the goal, pick the most tolerable \
format (e.g. 30/15s instead of 4-5 min VO2 reps) rather than dropping it, and revisit a "didn't work" fact older than \
~3 months or after a clear fitness change.
- Choose outdoor rides from the season and daylight: weekend outdoor rides when the season allows, weekday \
outdoor rides only with daylight after work (sunset well after 18:00). Otherwise use Zwift. The athlete swaps \
an outdoor ride for Zwift themselves if the weather turns, so give outdoor days a Zwift-friendly purpose.
- Outdoor rides: say road or gravel, give a terrain hint.
- Zwift workouts: complete step lists with warmup and cooldown; step durations must add up to \
duration_min. Use cadence targets sparingly and purposefully.
- Zwift on-screen messages: a step's text shows as the step starts, and its cues show later in the step \
(offset_s from the step start; for intervals from the start of the whole block, so rep k starts at \
(k-1) × (on_s + off_s)). Write them in your own coaching voice: execution instructions, the reason for the \
effort, and encouragement through the hard parts. Under 70 characters each, at least 30 s apart, about one \
every 3-5 min on steady work, and one in the closing stretch of long hard efforts. Rep counters are added \
automatically, so don't write "rep 2 of 5". Never tell the rider to exceed a step's target power: on \
hard efforts, cue holding the target, form and pacing; on easy or recovery steps, cue staying easy, fuelling \
and cadence, not effort.
- target_tss: your estimate for the session (rest = 0).
- FTP tests: plan one only when the athlete asks for it, or when your philosophy says a test is due and \
snapshot.ftp_tests shows no completed test (done) in the last 4 weeks. Use the protocol in your philosophy, set \
test_protocol, and make the day before rest or easy. A test day counts as a hard day; in a recovery week the \
test is the one hard session. Build all-out time trials (20-min, 8-min) as freeride steps so ERG doesn't cap \
them; the ramp test stays as steady 1-minute ERG steps. Explain the pacing in the description and step text. \
Never plan a test in the first days back after illness or an absence.
- Write titles and descriptions in English, concise and specific — like a coach's note, not marketing.
{style_rules}
Follow this coaching philosophy for everything else — intensity distribution, session choice, weekly \
structure and the voice of your notes:

{philosophy}"""


def system_prompt(c: coaches.Coach, rules: dict) -> str:
    style = [f"- At most {rules['max_hard_sessions']} hard sessions in a build week, including rides already done."]
    style.append("- Never two hard days in a row." if rules["max_consecutive_hard"] == 1 else
                 f"- At most {rules['max_consecutive_hard']} hard days in a row, and the day after is rest or easy.")
    return SYSTEM.format(style_rules="\n".join(style), philosophy=c.philosophy, health_rules=HEALTH_RULES)


def is_hard_day(d: dict) -> bool:
    if d["kind"] == "rest":
        return False
    if d["intensity"] in HARD or d.get("test_protocol", "none") != "none":
        return True
    if d["kind"] == "zwift":
        return workouts.is_hard(d["steps"])
    return d["intensity"] == "mixed"


def hard_errors(days: list[dict], done_rides: list[dict], rules: dict, recovery: bool) -> list[str]:
    """Hard-day count for the week and runs of consecutive hard days, counting rides already done."""
    errors = []
    max_hard, max_run = rules["max_hard_sessions"], rules["max_consecutive_hard"]
    if recovery:
        max_hard, max_run = min(max_hard, 1), 1
        errors += [f"{d['date']}: no {d['intensity']} in a recovery week" for d in days  # a test is the one touch
                   if d["kind"] != "rest" and d["intensity"] in {"vo2max", "anaerobic"}
                   and d.get("test_protocol", "none") == "none"]
    hard: dict[str, bool] = {}
    for r in done_rides:  # whole-ride IF misses many test rides, hence maximal_effort
        hard[r["date"]] = hard.get(r["date"], False) or (r.get("if") or 0) >= 0.85 or bool(r.get("maximal_effort"))
    hard |= {d["date"]: is_hard_day(d) for d in days}
    for d in days:
        before = (date.fromisoformat(d["date"]) - timedelta(days=1)).isoformat()
        if d.get("test_protocol", "none") != "none" and hard.get(before):
            errors.append(f"{d['date']}: the day before an FTP test must be rest or easy")
    week_start = (date.fromisoformat(days[0]["date"]) - timedelta(days=date.fromisoformat(days[0]["date"]).weekday()))
    n = sum(h for day_iso, h in hard.items() if day_iso >= week_start.isoformat())
    if n > max_hard:
        errors.append(f"{n} hard days this week (incl. rides already done); "
                      f"at most {max_hard}{' in a recovery week' if recovery else ''}")
    day, end, run = min(date.fromisoformat(k) for k in hard), date.fromisoformat(days[-1]["date"]), 0
    reported = False
    while day <= end:  # a run of rides already done is history: only report one that reaches a planned day
        run = run + 1 if hard.get(day.isoformat()) else 0
        reported = reported and run > 0
        if run > max_run and day.isoformat() >= days[0]["date"] and not reported:
            errors.append(f"{run} hard days in a row ending {day.isoformat()}; at most {max_run}")
            reported = True
        day += timedelta(days=1)
    return errors


def weeks_with_coach(db: DB, coach_key: str, week_start: date) -> int:
    """Consecutive weeks before this one whose active plan came from this coach."""
    n = 0
    while (p := db.active_plan((week_start - timedelta(weeks=n + 1)).isoformat())) and p["coach"] == coach_key:
        n += 1
    return n


def block_week(db: DB, week_start: date) -> int:
    start = db.get("block_start")
    if not start:
        db.set("block_start", week_start.isoformat())
        return 1
    weeks = (week_start - date.fromisoformat(start)).days // 7
    return weeks % 4 + 1


def validate(plan: dict, dates: list[str], cfg: Config, ctl: float, done_tss: float, recovery: bool,
             rules: dict | None = None, done_rides: list[dict] = (), notes: list[dict] = ()) -> list[str]:
    """Hard checks the plan must pass. Also normalises Zwift TSS to the profile estimate (in place).

    done_rides: rides from yesterday and earlier this week, for the hard-day rules.
    notes: coach notes. Absence days must be rest; event days may run past the availability limit."""
    rules = rules or coaches.get("nextlevel").rules(cfg.athlete_level)
    errors = []
    days = plan["days"]
    if [d["date"] for d in days] != dates:
        errors.append(f"days must be exactly these dates in order: {dates}")
        return errors
    away, events = note_days(notes, "absence"), note_days(notes, "event")
    for d in days:
        wd = date.fromisoformat(d["date"]).weekday()
        limit = cfg.max_minutes(wd)
        if d["date"] in away and d["kind"] != "rest":
            errors.append(f"{d['date']}: the athlete is away (coach note), so this must be a rest day")
        if d["kind"] != "rest" and d["duration_min"] > limit and d["date"] not in events:
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
            protocol = d.get("test_protocol", "none")
            d["target_tss"] = round(workouts.estimated_tss(d["steps"]) if protocol == "none"
                                    else ftptest.estimated_tss(protocol, d["steps"]))
        elif d["steps"]:
            d["steps"] = []
        if d["kind"] == "rest":
            d["target_tss"] = 0
    errors += hard_errors(days, list(done_rides), rules, recovery)
    week_tss = done_tss + sum(d["target_tss"] for d in days)
    cap = max(250, 7 * ctl * 0.75) if recovery else max(350, 7 * ctl + 280)
    if week_tss > cap:
        errors.append(f"week TSS {week_tss:.0f} (incl. {done_tss:.0f} already done) exceeds cap {cap:.0f}"
                      + (" for a recovery week" if recovery else ""))
    return errors


def note_days(notes: list[dict], kind: str) -> set[str]:
    out = set()
    for n in notes:
        if n["kind"] == kind:
            day, end = date.fromisoformat(n["start"]), date.fromisoformat(n["end"])
            while day <= end:
                out.add(day.isoformat())
                day += timedelta(days=1)
    return out


def done_this_week(snapshot: dict, week_start: date, first: date) -> tuple[list[dict], float, list[dict]]:
    """(rides done this week before `first`, their TSS, rides from yesterday on for the back-to-back rule)."""
    done = [r for r in snapshot["recent_rides"] if week_start.isoformat() <= r["date"] < first.isoformat()]
    since = min(week_start, first - timedelta(days=1)).isoformat()  # yesterday counts for back-to-back days
    done_rides = [r for r in snapshot["recent_rides"] if since <= r["date"] < first.isoformat()]
    return done, sum(r["tss"] or 0 for r in done), done_rides


async def run_validated(cfg: Config, db: DB, system: str, messages: list[dict], schema: dict, check, *,
                        purpose: str, effort: str = "high", max_tokens: int = 32000) -> tuple[dict, list[str]]:
    """Ask for structured output, run `check(result) -> errors`, and give the model one round to fix them.
    Returns the last result and the errors it still has."""
    result, errors = {}, []
    for attempt in range(2):
        msg = await llm.run(cfg.anthropic_api_key, system, messages, schema=schema, effort=effort,
                            max_tokens=max_tokens, db=db, purpose=purpose)
        result = json.loads(llm.text_of(msg))
        errors = check(result)
        if not errors or attempt == 1:
            break
        log.info("%s attempt %d failed validation: %s", purpose, attempt + 1, errors)
        messages = messages + [
            {"role": "assistant", "content": msg.content},
            {"role": "user", "content": "That failed these checks; return a corrected full response:\n- " + "\n- ".join(errors)},
        ]
    return result, errors


async def generate_plan(db: DB, cfg: Config, snapshot: dict, week_start: date, today: date,
                        coach_key: str | None = None) -> int:
    """Plan from max(today, week_start) through Sunday. Returns the stored plan id."""
    first = max(today, week_start)
    dates = [(first + timedelta(days=i)).isoformat() for i in range((week_start + timedelta(days=6) - first).days + 1)]
    bw = block_week(db, week_start)
    recovery = bw == 4
    done, done_tss, done_rides = done_this_week(snapshot, week_start, first)
    c = coaches.get(coach_key or cfg.default_coach)
    weeks = weeks_with_coach(db, c.key, week_start)
    rules = c.rules(cfg.athlete_level, weeks)

    request = {
        "plan_dates": dates,
        "block_week": bw,
        "block_week_type": "recovery" if recovery else "build",
        "coach": c.name,
        "weeks_with_this_coach": weeks,
        "already_done_this_week": done,
        "snapshot": memory.for_coach(snapshot, c),
    }
    messages = [{"role": "user", "content": (
        f"Plan these days: {', '.join(dates)}. This is week {bw} of the 4-week block "
        f"({'recovery' if recovery else 'build'} week).\n\n```json\n{json.dumps(request, indent=1)}\n```")}]
    plan, warnings = await run_validated(
        cfg, db, system_prompt(c, rules), messages, PLAN_SCHEMA, purpose="plan",
        check=lambda p: validate(p, dates, cfg, snapshot["load"]["ctl"], done_tss, recovery, rules, done_rides,
                                 snapshot.get("coach_notes", [])))
    plan["block_week"] = bw
    return db.save_plan(week_start.isoformat(), datetime.now().isoformat(timespec="seconds"),
                        llm.MODELS["plan"], plan, warnings, c.key)


def compliance(plan_day: dict, acts_that_day: list[dict]) -> str:
    """done | partial | missed | extra | rest | upcoming."""
    if plan_day["kind"] == "rest":
        return "extra" if acts_that_day else "rest"
    if not acts_that_day:
        return "missed"
    minutes = sum(a["moving_time"] or 0 for a in acts_that_day) / 60
    return "done" if minutes >= 0.75 * plan_day["duration_min"] else "partial"
