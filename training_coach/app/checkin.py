"""The coach's daily check-in: a short message in the Coach chat the first time the athlete opens the add-on each day.

Like ride feedback, the facts (what was planned and ridden since the last check-in, swaps, excuses, today, this week,
form, readiness) are computed here and the model only writes, so Haiku is enough. One call a day, ~3k tokens in.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from . import coaches, health, llm, memory, overview, stats
from .config import Config
from .db import DB

LOOKBACK_DAYS = 7     # at most this far back after days without a visit
SWAP_SHARE = 0.75     # a ride on another day of the week this long (of the session's minutes) counts as the session
EXCUSING_NOTES = {"absence", "injury", "other"}
MESSAGES = 8          # the athlete's own chat messages since the last check-in, for illness or travel never saved as a note

SCHEMA = {
    "type": "object",
    "properties": {"message": {"type": "string", "description": "2-4 short sentences, plain text"}},
    "required": ["message"],
    "additionalProperties": False,
}

SYSTEM = """You are {coach}, coaching one amateur road and gravel cyclist with a job and a life next to the bike. \
They just opened their training app for the first time today, and you leave them a short check-in message in your \
chat. The point of it is to make them want to train: a good feeling, not a report card. You get a JSON with the days \
since your last check-in (since_last_checkin), today, this week so far, their form, readiness from their health data \
(when present), your saved notes and what they wrote to you lately.

Write 2-4 short sentences, plain text, as a warm, friendly chat message in your own voice:
- Start with what went well since your last check-in, with one specific number (TSS, minutes, NP). "done" sessions, \
several in a row, a "swapped" session (ridden on another day, ridden_on) and back_on_bike (they rode in the days after \
a missed one) all deserve real appreciation: moving sessions around and getting going again is exactly right.
- Missed sessions are normal for an amateur and the plan is a guide, not a contract. Mention a miss at most briefly \
and without any guilt, blame or teasing, then look forward. Never suggest making up a missed session or cramming \
extra volume in. adjust_offered true: an "Adjust my week" button sits below your message, so you may say they can \
tap it and you'll rebalance the rest of the week. adjust_offered false: there's no button, don't mention one.
- excused_by (illness, travel, injury) or something they wrote in recent_messages: be kind about it. \
ask_how_they_are true: ask how they're doing; don't talk about the plan beyond today or mention the button.
- "partial" means they rode less than planned: that still counts. "extra" is a ride on a rest day: enjoyable, \
mention it only if it matters for recovery.
- Then today. today.status "done" or "partial": they've already ridden today, so celebrate it and point to recovery \
or the next session. "done_early": they rode today's session early (ridden_on), so today is easy or rest. Otherwise \
name today's session and one thing that makes it go well. today.session "Rest": rest is the job (if today.status is \
"rode", they rode anyway: keep it easy). today.session null: there's no plan this week; suggest planning one.
- readiness: only trends with enough data, each with signal true where a number is off (signals counts them). \
2 or more signals: say today's hard session would be better as endurance or rest, and that they can change it in the \
chat. 1 signal: ride today's session at the low end of its targets. Skip this if today's session is already easy or \
rest, or its description already adjusts for it. No signal: don't bring up health.
- form is context: the planner already took it into account, so it's not a reason to change today's session.
- last_sync is when rides were last synced; a ride after that isn't in the data yet, so don't count a session as \
missed if its day ended after it.
- Use only the facts in the JSON. No emoji, no headings, no sign-off, no exclamation mark in every sentence.
- first_checkin true: it's your first check-in with them, so say in a few words that you'll check in like this daily.
- English, in your own coaching voice:

{philosophy}"""


def _rides(acts: list[dict], day: str) -> list[dict]:
    return [{"name": a["name"], "minutes": round((a["moving_time"] or 0) / 60), "tss": a["tss"],
             "np_w": round(a["np"]) if a.get("np") else None} for a in acts if a["start_date_local"][:10] == day]


def _minutes(acts: list[dict], day: str) -> float:
    return sum(a["moving_time"] or 0 for a in acts if a["start_date_local"][:10] == day) / 60


def week_days(plan: dict, acts: list[dict], upto: date, today: date) -> dict[str, dict]:
    """Date → {plan day, status} for the plan's days up to `upto`, generous to an amateur who moves sessions around:
    - a missed session whose duration was ridden on a rest day of the same week (before or after) is "swapped", with
      ridden_on, and that rest day is "swapped_in" (a ride named after another session is already matched);
    - otherwise a missed session with a ride on one of the two days after it gets back_on_bike (they got going again);
    - today's session ridden early on a rest day is "done_early"."""
    out = {d["date"]: {"day": d, "status": s} for d, s in overview.day_statuses(plan, acts, upto.isoformat())}
    spare = sorted(k for k, v in out.items() if v["status"] == "extra")
    for k, v in sorted(out.items()):
        d = v["day"]
        if v["status"] != "missed" or not d["duration_min"] or k > today.isoformat():
            continue
        # Today's session can only have been ridden early; a past one before or after
        pool = [x for x in spare if x < k] if k == today.isoformat() else spare
        hit = next((x for x in pool if _minutes(acts, x) >= SWAP_SHARE * d["duration_min"]), None)
        if hit:
            spare.remove(hit)
            v.update(status="done_early" if k == today.isoformat() else "swapped", ridden_on=hit)
            out[hit].update(status="swapped_in", swapped_for=d["title"])
        elif k < today.isoformat():
            after = [(date.fromisoformat(k) + timedelta(days=n)).isoformat() for n in (1, 2)]
            if back := [x for x in after if x <= today.isoformat() and _minutes(acts, x)]:
                v["back_on_bike"] = back
    return out


def excuses(db: DB, first: date, last: date) -> dict[str, str]:
    """Day → the text of a note that covered it (absence, injury, other), also notes removed since: "I'm better, drop
    the note" mustn't turn the sick days into missed ones."""
    out: dict[str, str] = {}
    for n in db.notes_between(first.isoformat(), last.isoformat()):
        if n["kind"] not in EXCUSING_NOTES:
            continue
        day, end = max(date.fromisoformat(n["start"]), first), min(date.fromisoformat(n["end"]), last)
        while day <= end:
            out.setdefault(day.isoformat(), n["text"])
            day += timedelta(days=1)
    return out


def window_start(db: DB, today: date) -> date:
    """The last check-in's day (its session may have come after it), at most a week back."""
    last = db.get("checkin_day")
    return max(date.fromisoformat(last) if last else today - timedelta(days=1), today - timedelta(days=LOOKBACK_DAYS))


def since_last(db: DB, acts: list[dict], today: date, first: date | None = None) -> list[dict]:
    """Every day from `first` (default: window_start) to yesterday, with the active plan's session that day and how
    it went. Unridden rest days are left out."""
    first = first or window_start(db, today)
    excused = excuses(db, first, today - timedelta(days=1))
    out, weeks = [], {}
    day = first
    while day < today:
        ws = stats.week_start(day).isoformat()
        if ws not in weeks:
            plan = db.active_plan(ws)
            weeks[ws] = week_days(plan, acts, min(today, stats.week_start(day) + timedelta(days=6)), today) if plan else {}
        iso, item = day.isoformat(), {"date": day.isoformat(), "weekday": day.strftime("%A")}
        if v := weeks[ws].get(iso):
            d = v["day"]
            item |= {"planned": d["title"] if d["kind"] != "rest" else "Rest", "status": v["status"]}
            if d["kind"] != "rest":
                item |= {"intensity": d["intensity"], "duration_min": d["duration_min"]}
            item |= {k: v[k] for k in ("ridden_on", "swapped_for", "back_on_bike") if k in v}
        else:
            item |= {"planned": None, "status": "unplanned" if _rides(acts, iso) else "rest"}
        if item["status"] == "rest" and not _rides(acts, iso):
            day += timedelta(days=1)
            continue  # nothing to say about an unridden rest day
        item["rides"] = _rides(acts, iso)
        if iso in excused:
            item["excused_by"] = excused[iso]
        out.append(item)
        day += timedelta(days=1)
    return out


def today_block(db: DB, acts: list[dict], today: date) -> dict:
    plan = db.active_plan(stats.week_start(today).isoformat())
    s = overview.today_session(db, today)
    rides = _rides(acts, today.isoformat())
    out: dict = {"session": {k: s.get(k) for k in ("title", "kind", "duration_min", "intensity", "description", "terrain")}
                 if s else ("Rest" if plan else None), "rides": rides}
    if s:
        v = week_days(plan, acts, today, today)[today.isoformat()]
        out["status"] = v["status"] if v["status"] in ("done", "partial", "done_early") else "not yet"
        if "ridden_on" in v:
            out["ridden_on"] = v["ridden_on"]
    else:
        out["status"] = "rode" if rides else "not yet"
    return out


def worth_it(db: DB, acts: list[dict], today: date) -> bool:
    """Something to talk about: a plan this week or a ride in the last week."""
    recent = (today - timedelta(days=LOOKBACK_DAYS)).isoformat()
    return bool(db.active_plan(stats.week_start(today).isoformat())) or any(a["start_date_local"][:10] >= recent for a in acts)


def context(db: DB, cfg: Config, today: date) -> tuple[dict, coaches.Coach]:
    acts = db.activities(since_local=(today - timedelta(days=400)).isoformat())
    c = memory.coach_in_charge(db, cfg, datetime.combine(today, datetime.now().time()))
    d = stats.dashboard(acts, today, cfg)
    w = overview.week(db, acts, today)
    label, _ = overview.form_label(d["tsb"])
    first = window_start(db, today)
    days = since_last(db, acts, today, first)
    ctx = {
        "today_date": today.isoformat(), "weekday": today.strftime("%A"),
        "first_checkin": db.get("checkin_day") is None,
        "last_sync": (db.get("last_sync") or {}).get("at"),
        "since_last_checkin": days,
        "today": today_block(db, acts, today),
        "this_week": {k: w[k] for k in ("sessions", "done", "partial", "missed", "tss_done", "tss_planned", "focus")}
        if w["plan"] else {"plan": None, "tss_done": w["tss_done"], "rides": w["rides"]},
        "form": {"tsb": round(d["tsb"]), "state": label, "ctl": round(d["ctl"])},
        "athlete": {"goals": cfg.goals.strip() or "Not stated", "level": cfg.athlete_level},
        "coach_notes": [{k: n[k] for k in ("kind", "start", "end", "text")} for n in db.active_notes(today.isoformat())],
        "recent_messages": [m["content"][:300] for m in db.chat(50) if m["role"] == "user" and m["ts"][:10] >= first.isoformat()][-MESSAGES:],
        "memory": [f["text"] for f in db.memories()[-memory.MAX_FACTS:]],
    }
    signals = 0
    if health.entities(cfg):
        ready = health.readiness(health.summary(db, today))
        signals = sum(r["signal"] for r in ready.values())
        ctx["readiness"] = ready | {"signals": signals}
    # Several sessions in a row missed this past week, or a body under strain: ask how they are (may be illness)
    run = best = 0
    for x in since_last(db, acts, today, today - timedelta(days=LOOKBACK_DAYS)):
        if x["status"] == "missed" and "excused_by" not in x and "back_on_bike" not in x:
            run += 1
        elif x["status"] != "rest":
            run = 0
        best = max(best, run)
    ctx["ask_how_they_are"] = bool(signals) or best >= 2
    ctx["adjust_offered"] = adjust_request(db, ctx, today) is not None  # the prompt mentions the button only then
    return ctx, c


def adjust_request(db: DB, ctx: dict, today: date) -> str | None:
    """The chat message behind the check-in's "Adjust my week" button: a session this week was missed and not made
    up, and the plan has a session left after today to rebalance around. Last week's misses don't reshuffle this week."""
    ws = stats.week_start(today).isoformat()
    missed = [d["planned"] for d in ctx["since_last_checkin"]
              if d["status"] == "missed" and "excused_by" not in d and d["date"] >= ws]
    plan = db.active_plan(ws)
    later = [d for d in (plan or {}).get("payload", {}).get("days", []) if d["date"] > today.isoformat() and d["kind"] != "rest"]
    if not missed or not later:
        return None
    return (f"I missed {', '.join(dict.fromkeys(missed))}. Can you adjust the rest of my week so it still works, "
            "without cramming the missed session in?")


async def generate(db: DB, cfg: Config, today: date) -> int:
    """Writes today's check-in into the chat. Returns the message id."""
    ctx, c = context(db, cfg, today)
    msg = await llm.run(cfg.anthropic_api_key, SYSTEM.format(coach=c.name, philosophy=c.philosophy),
                        [{"role": "user", "content": f"```json\n{json.dumps(ctx, indent=1)}\n```"}],
                        schema=SCHEMA, effort="low", max_tokens=4000, db=db, purpose="checkin")
    text = json.loads(llm.text_of(msg))["message"].strip()
    msg_id = db.add_chat(datetime.now().isoformat(timespec="seconds"), "assistant", c.key, text, kind="checkin",
                         ask=adjust_request(db, ctx, today))
    db.set("checkin_day", today.isoformat())
    return msg_id
