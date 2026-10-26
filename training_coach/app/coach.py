"""Weekly plan generation: snapshot → Claude (structured output) → validation → stored plan."""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta

from . import coaches, ftptest, health, llm, memory, sports, workouts
from .config import Config
from .db import DB

log = logging.getLogger(__name__)

HARD = {"threshold", "vo2max", "anaerobic"}
INTENSITIES = ["recovery", "endurance", "tempo", "sweetspot", "threshold", "vo2max", "anaerobic", "mixed"]
RUN_INTENSITIES = ["recovery", "endurance", "long", "tempo", "threshold", "vo2max", "hills", "mixed"]
RUN_HARD = {"threshold", "vo2max", "hills", "mixed"}  # mixed: a fartlek or a race
SURFACES = ["none", "road", "trail", "track", "treadmill"]

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

CYCLING_DAY = {
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
}

RUN_DAY = {
    "type": "object",
    "properties": {
        "date": {"type": "string", "description": "YYYY-MM-DD"},
        "kind": {"type": "string", "enum": ["rest", "run"]},
        "title": {"type": "string"},
        "description": {"type": "string", "description": "Purpose, the session's structure (reps, recoveries), HR or "
                                                         "pace ceilings, strides or strength work, and the short version"},
        "duration_min": {"type": "integer"},
        "intensity": {"type": "string", "enum": RUN_INTENSITIES},
        "target_tss": {"type": "integer"},
        "target_pace": {"type": "string", "description": "Pace range for the main part, e.g. '5:10–5:25/km'; empty for rest"},
        "surface": {"type": "string", "enum": SURFACES, "description": "none for rest"},
    },
    "required": ["date", "kind", "title", "description", "duration_min", "intensity", "target_tss", "target_pace",
                 "surface"],
    "additionalProperties": False,
}
DAY_SCHEMAS = {"cycling": CYCLING_DAY, "running": RUN_DAY}


def plan_schema(sport: str = "cycling") -> dict:
    return {
        "type": "object",
        "properties": {
            "week_focus": {"type": "string"},
            "rationale": {"type": "string", "description": "Why this week looks like this, 3-6 sentences"},
            "days": {"type": "array", "items": DAY_SCHEMAS[sport]},
        },
        "required": ["week_focus", "rationale", "days"],
        "additionalProperties": False,
    }


PLAN_SCHEMA = plan_schema("cycling")

HEALTH_RULES = """- snapshot.health, when present, holds health trends from health_source (Apple Health or Garmin): the last \
7 days against a baseline of the 28 days \
before them, with the days of data in each. Read them as a readiness signal, never from a single day, and only \
when the 7 days hold at least {min_days_7d} days of data and the baseline at least {min_days_baseline}. Signals: HRV \
change_pct about {hrv_drop_pct}% or lower, resting HR change +{rhr_rise_bpm} bpm or more, 7-day sleep under ~{sleep_min_h} h. \
Two signals together: swap the next hard \
session for endurance or rest and say why. One signal: keep the session but shorten it or cap it at the low \
end of its targets, and mention it. A sharp HRV rise during heavy load isn't freshness. Weight is only for \
W/kg, and Apple's VO2 max comes from walking and running, so neither is a readiness signal. HRV in ms is only \
comparable with the athlete's own baseline (Garmin: overnight rMSSD average; Apple: spot SDNN), never across \
sources or with population norms: only change_pct counts. Missing data means \
nothing.""".format(**health.READINESS)

INTRO = {"cycling": """You are a cycling coach for a single amateur road and gravel cyclist in Mälardalen, Sweden. \
They train on Zwift indoors and ride road/gravel outside. Training is organised as rolling 4-week blocks \
(3 build weeks + 1 recovery week).

You receive a JSON snapshot of their training (load metrics, power curve, recent rides, season and \
daylight, availability) and plan the requested days. snapshot.athlete holds their own goals, self-rated \
level and the number of sessions per week they aim for. Plan toward those goals. Treat sessions per week as \
a target, not a rule: recovery weeks or high fatigue may justify fewer.""",
         "running": """You are a running coach for a single amateur athlete in Mälardalen, Sweden, who may also ride a \
bike (see snapshot.sports). They run on roads, trails and, when it's dark or icy, a treadmill. Training is organised as \
rolling 4-week blocks (3 build weeks + 1 recovery week), shared with the bike when they do both.

You receive a JSON snapshot of their training (load metrics shared across sports, recent activities with their sport, \
snapshot.running with their run history and threshold pace and HR, season and daylight, availability) and plan the \
requested days. snapshot.athlete holds their own goals and self-rated level (a cycling W/kg level: running needs its \
own judgement from snapshot.running) and the number of sessions per week they aim for, across sports. Plan toward \
those goals."""}

SHARED_RULES = """- Respect form: if TSB is below -25, reduce load; if above +10, the athlete can absorb more.
- Keep CTL ramp at roughly +3 to +6 per week in build weeks. Recovery weeks ~60-70% of a build week's TSS, \
at most one hard session and no VO2max/anaerobic.
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
- target_tss: your estimate for the session (rest = 0).
"""

SPORT_RULES = {"cycling": """- Base intensities on the given FTP. Express all Zwift power as fractions of FTP.
- Openers (under 5 min in total above 105% FTP) are labelled endurance.\n- A day counts as hard if it has ≥10 min at ≥88% FTP or ≥5 min above 105% FTP (whatever you label it), \
an intensity of threshold/VO2max/anaerobic, or is an outdoor "mixed" ride. Rides already done count too \
(IF ≥ 0.85 or maximal_effort is hard), including yesterday's.
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
- FTP tests: plan one only when the athlete asks for it, or when your philosophy says a test is due and \
snapshot.ftp_tests shows no completed test (done) in the last 4 weeks. Use the protocol in your philosophy, set \
test_protocol, and make the day before rest or easy. A test day counts as a hard day; in a recovery week the \
test is the one hard session. Build all-out time trials (20-min, 8-min) as freeride steps so ERG doesn't cap \
them; the ramp test stays as steady 1-minute ERG steps. Explain the pacing in the description and step text. \
Never plan a test in the first days back after illness or an absence.
""",
               "running": """- Express run intensity as HR (against snapshot.running.threshold_hr) and pace (against \
snapshot.running.threshold_pace, when known). Put a pace range in target_pace and an HR ceiling in the description.
- Easy runs stay under ~85% of threshold HR. A run counts as hard if its intensity is threshold, vo2max, hills or \
mixed (fartlek, race). Tempo means steady running at or below ~88% of threshold HR; anything at threshold pace or HR \
(a 20-min tempo run at threshold pace included) is labelled threshold. Long runs, easy runs and strides aren't hard. \
Rides and runs already done count too (a ride with IF ≥ 0.85 or maximal_effort, a run with HR IF ≥ 0.9), including \
yesterday's.
- Run durability: run volume comes from running history (snapshot.running), never from CTL, which a cyclist's riding \
inflates. The week's run minutes (runs already done this week included) stay within snapshot.running.cap_minutes \
(1.1 × the last 4 weeks' average, or a return-to-run floor), and no run is longer than \
snapshot.running.longest_run_cap_min (1.1 × the longest run of the last 30 days, at least 30 min): one run much \
longer than usual is the biggest injury risk. At most snapshot.running.max_hard_runs hard runs a week, and none \
while snapshot.running.returning is true (the first 2 weeks back after 4+ weeks without running; strides are fine). \
In a recovery week, no hills either. Strong cycling fitness doesn't excuse impact load: tendons and bones adapt \
slower than the heart.
- Surface: road or trail outdoors in daylight; treadmill when the run falls after sunset (snapshot.season.sunset, \
weekdays after work) unless snapshot.running.dark_runs_ok, or in icy midwinter. Track only for VO2max work.
- No FTP tests and no steps: a run is text only. Put the structure (warm-up, reps, recoveries, cool-down), strides \
and strength work in the description, and the short version for a busy day.
"""}

TWO_SPORTS = """- They train two sports this week (snapshot.sports). Each day may hold one session of each sport if both fit the \
day's availability minutes together. {fixed_rule}
- Hard days count across sports: a VO2max run the day after bike intervals is two hard days in a row, and the \
weekly hard-day limit is for both sports together. The weekly TSS cap is for both sports together too.
"""

FIXED_RULE = {
    "first": "You are the primary coach: plan your sport first. The other sport's coach fills in around you, so leave \
some room for it on the days you don't use.",
    "second": "The primary coach already planned their sessions (other_sport_days, fixed: don't change them). Plan your \
sport around them: other_sport_minutes is what their session takes from each day's availability, and \
tss_budget_left is what the week's TSS cap leaves for you.",
    "chat": "The other sport's sessions in active_plan are another coach's: you may only change your own sport's days.",
}

SYSTEM = """{intro}

Hard rules, whichever coaching style you follow:
{shared}{sport_rules}{two_sports}- Write titles and descriptions in English, concise and specific — like a coach's note, not marketing.
{style_rules}
Follow this coaching philosophy for everything else — intensity distribution, session choice, weekly \
structure and the voice of your notes:

{philosophy}"""

def system_prompt(c: coaches.Coach, rules: dict, role: str | None = None) -> str:
    """`role`: None with one sport this week, else first | second (two-pass planning) or chat. With two sports, `rules`
    are the week's (the primary coach's): one hard-day limit for both sports, whichever plans first."""
    style = [f"- At most {rules['max_hard_sessions']} hard sessions in a build week, including sessions already done."]
    style.append("- Never two hard days in a row." if rules["max_consecutive_hard"] == 1 else
                 f"- At most {rules['max_consecutive_hard']} hard days in a row, and the day after is rest or easy.")
    two = TWO_SPORTS.format(fixed_rule=FIXED_RULE[role]) if role else ""
    return SYSTEM.format(intro=INTRO[c.sport], shared=SHARED_RULES.format(health_rules=HEALTH_RULES),
                         sport_rules=SPORT_RULES[c.sport], two_sports=two, style_rules="\n".join(style),
                         philosophy=c.philosophy)


def week_rules(db: DB, cfg: Config, week_start: date, day: date) -> dict:
    """The hard-day rules for a week: the primary active sport's coach's. With two sports both coaches work to one
    limit, so it can't depend on which of them plans first."""
    sport = cfg.active_sports(day)[0]
    c = coaches.get(cfg.coach_key(sport), sport)
    return c.rules(cfg.athlete_level, weeks_with_coach(db, c.key, week_start))


def is_hard_day(d: dict) -> bool:
    if d["kind"] == "rest":
        return False
    if sports.day_sport(d) == "running":
        return d["intensity"] in RUN_HARD
    if d["intensity"] in HARD or d.get("test_protocol", "none") != "none":
        return True
    if d["kind"] == "zwift":
        return workouts.is_hard(d["steps"])
    return d["intensity"] == "mixed"


def done_hard(r: dict) -> bool:
    """A session already done was hard: a ride at IF ≥ 0.85 or with a maximal effort (whole-ride IF misses many test
    rides), a run at HR IF ≥ 0.9 (HR over threshold HR: easy runs sit under ~0.85). A run scored on pace isn't judged
    (an estimated threshold pace makes its IF unreliable). Walks never."""
    sport = r.get("sport") or "ride"
    if sport == "walk" or (sport == "run" and r.get("tss_source") != "rhr"):
        return False
    return (r.get("if") or 0) >= (0.9 if sport == "run" else 0.85) or bool(r.get("maximal_effort"))


def hard_errors(days: list[dict], done_rides: list[dict], rules: dict, recovery: bool) -> list[str]:
    """Hard-day count for the week and runs of consecutive hard days, counting sessions already done. `days` may hold
    two sessions on a date (a ride and a run): the date is hard if either is."""
    errors = []
    max_hard, max_run = rules["max_hard_sessions"], rules["max_consecutive_hard"]
    if recovery:
        max_hard, max_run = min(max_hard, 1), 1
        errors += [f"{d['date']}: no {d['intensity']} in a recovery week" for d in days  # a test is the one touch
                   if d["kind"] != "rest" and d.get("test_protocol", "none") == "none" and d["intensity"] in (
                       {"vo2max", "anaerobic", "hills"} if sports.day_sport(d) == "running" else {"vo2max", "anaerobic"})]
    hard: dict[str, bool] = {}
    for r in done_rides:
        hard[r["date"]] = hard.get(r["date"], False) or done_hard(r)
    for d in days:
        hard[d["date"]] = hard.get(d["date"], False) or is_hard_day(d)
    dates = sorted({d["date"] for d in days})
    for d in days:
        before = (date.fromisoformat(d["date"]) - timedelta(days=1)).isoformat()
        if d.get("test_protocol", "none") != "none" and hard.get(before):
            errors.append(f"{d['date']}: the day before an FTP test must be rest or easy")
    week_start = (date.fromisoformat(dates[0]) - timedelta(days=date.fromisoformat(dates[0]).weekday()))
    n = sum(h for day_iso, h in hard.items() if day_iso >= week_start.isoformat())
    if n > max_hard:
        errors.append(f"{n} hard days this week (incl. sessions already done, both sports); "
                      f"at most {max_hard}{' in a recovery week' if recovery else ''}")
    day, end, run = min(date.fromisoformat(k) for k in hard), date.fromisoformat(dates[-1]), 0
    reported = False
    while day <= end:  # a run of sessions already done is history: only report one that reaches a planned day
        run = run + 1 if hard.get(day.isoformat()) else 0
        reported = reported and run > 0
        if run > max_run and day.isoformat() >= dates[0] and not reported:
            errors.append(f"{run} hard days in a row ending {day.isoformat()}; at most {max_run}")
            reported = True
        day += timedelta(days=1)
    return errors


def run_errors(days: list[dict], done: list[dict], running: dict | None) -> list[str]:
    """The run durability guard (numbers from stats.running): the week's run minutes from running history, never from
    combined CTL; no single run much longer than the longest recent one; the hard-run cap, and no hard run in the first
    2 weeks back after a break. `done`: sessions done before the planned days (only this week's runs count)."""
    planned = [d for d in days if sports.day_sport(d) == "running" and d["kind"] != "rest"]
    if not running or not planned:
        return []
    first = date.fromisoformat(min(d["date"] for d in days))
    week_start = (first - timedelta(days=first.weekday())).isoformat()  # yesterday may be last Sunday: not this week
    done_runs = [r for r in done if r.get("sport") == "run" and r["date"] >= week_start]
    errors = []
    minutes = sum(d["duration_min"] for d in planned) + sum(r.get("minutes") or 0 for r in done_runs)
    if minutes > running["cap_minutes"]:
        errors.append(f"{minutes} run minutes this week (incl. runs done) exceed the run durability cap of "
                      f"{running['cap_minutes']} min (1.1 × the last 4 weeks' average, or the return-to-run floor): "
                      "impact load builds slower than fitness")
    errors += [f"{d['date']}: a {d['duration_min']} min run is longer than {running['longest_run_cap_min']} min "
               "(1.1 × the longest run of the last 30 days): one long jump in a single run is the biggest injury risk"
               for d in planned if d["duration_min"] > running["longest_run_cap_min"]]
    hard = sum(is_hard_day(d) for d in planned) + sum(done_hard(r) for r in done_runs)
    if running.get("returning") and hard:
        errors.append(f"{hard} hard run{'s' if hard > 1 else ''} this week; none in the first 2 weeks back after 4+ "
                      "weeks without running (strides are fine)")
    elif hard > running.get("max_hard_runs", 2):
        errors.append(f"{hard} hard runs this week; at most {running.get('max_hard_runs', 2)}")
    return errors


def coach_keys(plan: dict) -> set[str | None]:
    """Every coach with sessions in a plan: the plan's (primary) coach and each day's."""
    return {plan["coach"]} | {d.get("coach") for d in plan["payload"]["days"] if d.get("coach")}


def weeks_with_coach(db: DB, coach_key: str, week_start: date) -> int:
    """Consecutive weeks before this one whose active plan came from (or had sessions by) this coach."""
    n = 0
    while (p := db.active_plan((week_start - timedelta(weeks=n + 1)).isoformat())) and coach_key in coach_keys(p):
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
             rules: dict | None = None, done_rides: list[dict] = (), notes: list[dict] = (), fixed: list[dict] = (),
             running: dict | None = None) -> list[str]:
    """Hard checks the plan must pass. Also normalises Zwift TSS to the profile estimate (in place).

    plan["days"]: the days to check, sorted by date, at most one per sport and date, covering exactly `dates`.
    fixed: the other sport's days this week, already planned (they count for minutes, hard days, TSS and running).
    done_rides: sessions from yesterday and earlier this week, for the hard-day and run rules.
    notes: coach notes. Absence days must be rest; event days may run past the availability limit."""
    rules = rules or coaches.get("nextlevel").rules(cfg.athlete_level)
    errors = []
    days = plan["days"]
    seen = [d["date"] for d in days]
    keys = [sports.day_key(d) for d in days]
    if sorted(set(seen)) != dates or seen != sorted(seen) or len(set(keys)) != len(keys):
        errors.append(f"days must be exactly these dates in order, one per date: {dates}")
        return errors
    away, events = note_days(notes, "absence"), note_days(notes, "event")
    every = sorted([*days, *fixed], key=lambda d: d["date"])
    for d in days:
        if d["date"] in away and d["kind"] != "rest":
            errors.append(f"{d['date']}: the athlete is away (coach note), so this must be a rest day")
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
        elif d.get("steps"):
            d["steps"] = []
        if d["kind"] == "rest":
            d["target_tss"] = 0
    for day_iso in dates:  # both sports' sessions on a day must fit its minutes together
        on_day = [d for d in every if d["date"] == day_iso and d["kind"] != "rest"]
        minutes, limit = sum(d["duration_min"] for d in on_day), cfg.max_minutes(date.fromisoformat(day_iso).weekday())
        if on_day and minutes > limit and day_iso not in events:
            both = " (both sports together)" if len({sports.day_sport(d) for d in on_day}) > 1 else ""
            errors.append(f"{day_iso}: {minutes} min{both} exceeds availability {limit} min")
    errors += hard_errors(every, list(done_rides), rules, recovery)
    errors += run_errors(every, list(done_rides), running)
    week_tss = done_tss + sum(d["target_tss"] for d in every)
    cap = max(250, 7 * ctl * 0.75) if recovery else max(350, 7 * ctl + 280)
    if week_tss > cap:
        errors.append(f"week TSS {week_tss:.0f} (incl. {done_tss:.0f} already done) exceeds cap {cap:.0f}"
                      + (" for a recovery week" if recovery else "") + (" (both sports together)" if fixed else ""))
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
    """(sessions done this week before `first`, their TSS, sessions from yesterday on for the back-to-back rule).
    Rides, runs and walks: load is shared."""
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


def tag(days: list[dict], sport: str, coach_key: str) -> list[dict]:
    """A model's days → stored plan days: sport and coach on each, and the cycling fields runs don't have (so code that
    reads a day never needs to ask its sport first)."""
    for d in days:
        d["sport"], d["coach"] = sport, coach_key
        if sport == "running":
            d.setdefault("steps", [])
            d.setdefault("bike", "none")
            d.setdefault("terrain", "")
            d.setdefault("test_protocol", "none")
    return days


def merge_days(passes: list[list[dict]]) -> list[dict]:
    """One week from each sport's days, primary sport first: every session, and a rest day only on a date with no
    session at all."""
    out = []
    for day_iso in sorted({d["date"] for days in passes for d in days}):
        on_day = [d for days in passes for d in days if d["date"] == day_iso]
        sessions = [d for d in on_day if d["kind"] != "rest"]
        out += sessions or on_day[:1]
    return out


def sport_dates(cfg: Config, dates: list[str]) -> dict[str, list[str]]:
    """Each sport's dates to plan, primary first: a sport on hold, or past its "until" date, plans none."""
    out: dict[str, list[str]] = {}
    for d in dates:
        for s in cfg.active_sports(date.fromisoformat(d)):
            out.setdefault(s, []).append(d)
    return {s: out[s] for s in sorted(out, key=lambda s: s != cfg.primary_sport)}


async def generate_plan(db: DB, cfg: Config, snapshot: dict, week_start: date, today: date,
                        coach_key: str | None = None) -> int:
    """Plan from max(today, week_start) through Sunday. Returns the stored plan id.

    One sport active: that sport's coach plans, as before. Two: the primary sport's coach plans its days, then the
    other coach plans around them (two Sonnet calls), each checked against the combined rules. `coach_key` overrides the
    athlete's coach for its sport."""
    first = max(today, week_start)
    dates = [(first + timedelta(days=i)).isoformat() for i in range((week_start + timedelta(days=6) - first).days + 1)]
    bw = block_week(db, week_start)
    recovery = bw == 4
    done, done_tss, done_rides = done_this_week(snapshot, week_start, first)
    plan_for = sport_dates(cfg, dates)
    two = len(plan_for) > 1
    override = coaches.COACHES.get(coach_key or "")
    passes: list[list[dict]] = []
    payloads: dict[str, dict] = {}
    warnings: list[str] = []
    lead, rules = None, None
    for i, (sport, sdates) in enumerate(plan_for.items()):
        c = override if override and override.sport == sport else coaches.get(cfg.coach_key(sport), sport)
        lead = lead or c
        weeks = weeks_with_coach(db, c.key, week_start)
        # One hard-day limit for the week: the primary coach's, so it doesn't depend on which sport plans first
        rules = rules or c.rules(cfg.athlete_level, weeks)
        fixed = [d for days in passes for d in days]
        request = {
            "plan_dates": sdates,
            "sport": sport,
            "block_week": bw,
            "block_week_type": "recovery" if recovery else "build",
            "coach": c.name,
            "weeks_with_this_coach": weeks,
            "already_done_this_week": done,
        }
        if fixed:
            cap = max(250, 7 * snapshot["load"]["ctl"] * 0.75) if recovery else max(350, 7 * snapshot["load"]["ctl"] + 280)
            request |= {"other_sport_days": [{k: d[k] for k in ("date", "sport", "kind", "title", "duration_min",
                                                                 "intensity", "target_tss")} for d in fixed],
                        "other_sport_minutes": {d: sum(x["duration_min"] for x in fixed if x["date"] == d and x["kind"] != "rest")
                                                for d in sdates},
                        "tss_budget_left": round(cap - done_tss - sum(d["target_tss"] for d in fixed))}
        request["snapshot"] = memory.for_coach(snapshot, c)
        messages = [{"role": "user", "content": (
            f"Plan these {sport} days: {', '.join(sdates)}. This is week {bw} of the 4-week block "
            f"({'recovery' if recovery else 'build'} week).\n\n```json\n{json.dumps(request, indent=1)}\n```")}]

        def check(p, sport=sport, sdates=sdates, c=c, rules=rules, fixed=fixed):
            tag(p["days"], sport, c.key)
            return validate(p, sdates, cfg, snapshot["load"]["ctl"], done_tss, recovery, rules, done_rides,
                            snapshot.get("coach_notes", []), fixed, snapshot.get("running"))
        plan, errs = await run_validated(
            cfg, db, system_prompt(c, rules, ("first", "second")[min(i, 1)] if two else None),
            messages, plan_schema(sport), purpose="plan", check=check)
        tag(plan["days"], sport, c.key)
        passes.append(plan["days"])
        payloads[sport] = {"coach": c.key, "week_focus": plan["week_focus"], "rationale": plan["rationale"]}
        warnings += [f"{sports.SPORTS[sport]}: {e}" if two else e for e in errs]
    first_sport = next(iter(payloads))
    payload = {"week_focus": payloads[first_sport]["week_focus"], "rationale": payloads[first_sport]["rationale"],
               "days": merge_days(passes), "block_week": bw, "sports": payloads}
    return db.save_plan(week_start.isoformat(), datetime.now().isoformat(timespec="seconds"),
                        llm.MODELS["plan"], payload, warnings, lead.key)


WALK_MIN = 45  # a walk this long can stand in for an easy session


def counts_for(plan_day: dict, a: dict) -> bool:
    """An activity can be this planned session: a ride for a ride, a run for a run, and a walk of WALK_MIN or more for
    a recovery session only. Recovery is about moving gently, and a long walk does that; an endurance ride (IF ~0.7) or
    an easy run's impact loading is a stimulus a walk (IF ~0.45) doesn't give. A dict without a sport type (tests,
    older callers) counts as the session's own sport."""
    kind = sports.kind(a.get("sport_type"))
    if kind == "walk":
        return (a.get("moving_time") or 0) >= WALK_MIN * 60 and plan_day["intensity"] == "recovery"
    return kind == sports.ACTIVITY_KIND[sports.day_sport(plan_day)] or (kind is None and "sport_type" not in a)


def compliance(plan_day: dict, acts_that_day: list[dict]) -> str:
    """done | partial | missed | extra | rest | upcoming. `acts_that_day` may hold any sport: only the ones that can be
    this session count (counts_for). A rest day is "extra" only for a ride or run, never for a walk."""
    if plan_day["kind"] == "rest":
        return "extra" if any(sports.kind(a.get("sport_type")) != "walk" for a in acts_that_day) else "rest"
    acts = [a for a in acts_that_day if counts_for(plan_day, a)]
    if not acts:
        return "missed"
    minutes = sum(a["moving_time"] or 0 for a in acts) / 60
    return "done" if minutes >= 0.75 * plan_day["duration_min"] else "partial"
