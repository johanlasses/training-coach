"""Chat with the coach: answers, plan change proposals for the rest of the week, and lasting coach notes."""
from __future__ import annotations

import copy
import json
from datetime import date, datetime, time, timedelta

from . import coach, coaches, memory, sports
from .config import Config
from .db import DB
from .stats import week_start

HISTORY = 20  # earlier messages sent with each question

NOTE_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["absence", "event", "injury", "other"]},
        "start": {"type": "string", "description": "YYYY-MM-DD"},
        "end": {"type": "string", "description": "YYYY-MM-DD, the last day the note matters (the event day for events)"},
        "text": {"type": "string", "description": "Short and specific, e.g. 'B-race: 80 km gravel, wants to ride it hard'"},
    },
    "required": ["kind", "start", "end", "text"],
    "additionalProperties": False,
}

def chat_schema(sport: str = "cycling") -> dict:
    """The reply, with plan changes in the addressed coach's sport."""
    return CHAT_SCHEMA | {"properties": CHAT_SCHEMA["properties"] | {"plan_changes": {
        "type": "array", "items": coach.DAY_SCHEMAS[sport],
        "description": "Replacement days for open_dates that change, in your sport only; empty if the plan stays"}}}


CHAT_SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string", "description": "Your answer to the athlete, plain text"},
        "plan_changes": {"type": "array", "items": coach.CYCLING_DAY,
                         "description": "Replacement days for open_dates that change; empty if the plan stays"},
        "add_notes": {"type": "array", "items": NOTE_SCHEMA},
        "remove_note_ids": {"type": "array", "items": {"type": "integer"}},
        "remember": {"type": "array", "items": {"type": "string"},
                     "description": "New lasting facts about the athlete, one short fact per item"},
        "forget_memory_ids": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["reply", "plan_changes", "add_notes", "remove_note_ids", "remember", "forget_memory_ids"],
    "additionalProperties": False,
}

ADDENDUM = """

You are now in a chat with the athlete. Each message from them comes with fresh context: the snapshot, this \
week's active plan, the open dates you may still change, the coach notes you have saved and memory, the lasting \
facts you and earlier coaches learned about them.

- Answer in your own voice, plainly and briefly: a few short paragraphs at most unless they ask for detail. \
Use the numbers in the snapshot. If the data can't answer the question, say so.
- plan_changes: only when the athlete asks for a change or what they tell you requires one (time away, \
illness, an event, fatigue). Give complete replacement days, only for dates in open_dates, and only the days \
that change. The week after your change must still follow every rule above. In the reply, say what you changed \
and why; the athlete applies it with a button. If there is no active plan, leave plan_changes empty and tell \
them to generate a plan on the Plan tab first.
- add_notes: save what must shape plans beyond this conversation, such as days away (absence), a secondary \
event or race (event: put the distance, type and how much it matters in the text), an injury or illness, or a \
lasting constraint (other). Resolve dates like "next Friday" from snapshot.today. Don't save what is already \
in the notes, and don't save things that only change this week's plan.
- Missed sessions are normal for an amateur with a job and a life: the plan is a guide, not a contract. When they \
ask you to adjust after a miss, rebalance the remaining open days realistically: keep the week's most important \
session if it still fits with a rest or easy day around it, drop or shorten the rest, and never cram the missed work \
in or add volume to catch up. Be encouraging about it.
- remove_note_ids: notes the athlete says no longer apply.
- remember: lasting facts about the athlete that will still matter in months and that the profile doesn't hold: \
background and earlier training, what has or hasn't worked for them, preferences (indoor or outdoor, sessions they \
like or dread), equipment, recurring constraints such as shift work or family, chronic or recurring issues, and how \
they respond to load. One short fact per item, in their words where you can, with the context that makes it fair \
later, e.g. "Said in Oct 2026 that 3 interval days a week wore them down (CTL ~55)". A one-off remark from a bad \
week isn't a fact. Something with a date range is a note; a yearly pattern is memory, plus a note when it comes up. \
Don't remember this week's plan or what is already in memory.
- forget_memory_ids: facts that turned out wrong or no longer hold. When a fact changes, forget the old one and \
remember the new one.
- If active_plan.your_sport_planned_by isn't you, the athlete switched to you this week and that plan is your \
predecessor's. Acknowledge the handover the first time. Once you change anything, the rest of the week must pass your rules: if \
your predecessor's remaining days break them, ease those days too and say why.
- Their main goal is snapshot.athlete.goals. A smaller event doesn't replace it: save it as an event note and \
fit it into the progression. If the main goal itself changes, suggest they update Goals on the Profile page.
- If they ask for an FTP test, plan your own protocol (see FTP testing) on an open date with a rest or easy day \
before it, not in the first days after illness or an absence. If this week has no good slot, save a note \
(kind other) asking for a test early next week.
- Pain or injury: don't diagnose. Suggest easy riding or rest, and a physio if it persists. Illness: follow the \
illness rule above.
- open_dates leaves out today when today's session in your sport is already done.
- To drop one of your sessions, send a rest day for that date: the other sport's session that day (if any) stays."""


def open_dates(plan: dict | None, today: date, rode_today: bool = False) -> list[str]:
    """Plan dates the chat may still change: today on, without today once its session has been done."""
    first = (today + timedelta(days=1) if rode_today else today).isoformat()
    return sorted({d["date"] for d in (plan or {}).get("payload", {}).get("days", []) if d["date"] >= first})


def rest_day(day: str, sport: str, coach_key: str | None) -> dict:
    return {"date": day, "kind": "rest", "title": "Rest", "description": "", "duration_min": 0, "intensity": "recovery",
            "target_tss": 0, "bike": "none", "terrain": "", "steps": [], "test_protocol": "none", "sport": sport,
            "coach": coach_key}


def own_days(plan: dict, dates: list[str], sport: str) -> list[dict]:
    """A sport's days on `dates`, one per date: its session, else its rest day (a merged two-sport plan keeps a rest
    day only on a date without any session)."""
    days = {d["date"]: copy.deepcopy(d) for d in plan["payload"]["days"]
            if d["date"] in dates and (sports.day_sport(d) == sport or d["kind"] == "rest")}
    for d in days.values():
        if d["kind"] == "rest":
            d["sport"] = sport
    return [days.get(k) or rest_day(k, sport, memory.plan_coach(plan, sport)) for k in dates]


def merge(plan: dict, changes: list[dict], dates: list[str], sport: str = "cycling") -> list[dict]:
    """The sport's open days with the changed days swapped in."""
    by_date = {d["date"]: d for d in own_days(plan, dates, sport)}
    for d in changes:
        by_date[d["date"]] = copy.deepcopy(d)
    return [by_date[k] for k in sorted(by_date)]


def others(plan: dict, dates: list[str], sport: str) -> list[dict]:
    """The other sport's sessions on `dates`: fixed for this coach."""
    return [d for d in plan["payload"]["days"] if d["date"] in dates and d["kind"] != "rest" and sports.day_sport(d) != sport]


def replace_days(days: list[dict], changes: list[dict]) -> list[dict]:
    """A plan's days with a sport's changed days in: per changed date, that sport's session (or the date's rest day)
    goes, the change comes in unless it's a rest day next to the other sport's session."""
    changed = {sports.day_key(d) for d in changes}
    dates = {d["date"] for d in changes}
    kept = [d for d in days if sports.day_key(d) not in changed and not (d["kind"] == "rest" and d["date"] in dates)]
    return coach.merge_days([kept, [copy.deepcopy(d) for d in changes]])


def strip_cues(days: list[dict]) -> list[dict]:
    """Plan days without the on-screen cues, which the chat doesn't need and which cost tokens."""
    out = copy.deepcopy(days)
    for d in out:
        for s in d.get("steps", []):
            s.pop("cues", None)
    return out


def checker(plan: dict | None, dates: list[str], cfg: Config, snapshot: dict, today: date, rules: dict,
            c: coaches.Coach | None = None):
    """check(result) → errors for a chat response's plan changes in coach `c`'s sport, with the week's rules."""
    c = c or coaches.get(None)

    def check(result: dict) -> list[str]:
        changes = coach.tag(result["plan_changes"], c.sport, c.key)
        if not changes:
            return []
        if not plan:
            return ["there is no active plan this week: plan_changes must be empty"]
        wrong = [d["date"] for d in changes if d["date"] not in dates]
        if wrong:
            return [f"plan_changes may only use these dates: {dates} (got {wrong})"]
        if len({d["date"] for d in changes}) != len(changes):
            return ["each date may appear once in plan_changes"]
        week_start = date.fromisoformat(plan["week_start"])
        # From the first open date: a ride already done today counts as done, not as a plan day
        _, done_tss, done_rides = coach.done_this_week(snapshot, week_start, date.fromisoformat(dates[0]))
        merged = {"days": merge(plan, changes, dates, c.sport)}
        errors = coach.validate(merged, dates, cfg, snapshot["load"]["ctl"], done_tss,
                                plan["payload"].get("block_week") == 4, rules, done_rides, snapshot.get("coach_notes", []),
                                others(plan, dates, c.sport), snapshot.get("running"))
        fixed = {d["date"]: d for d in merged["days"]}
        result["plan_changes"] = [fixed[d["date"]] for d in changes]  # with validate's TSS normalisation
        return errors
    return check


def chat_coach(db: DB, cfg: Config, today: date, sport: str | None = None) -> coaches.Coach:
    return memory.coach_in_charge(db, cfg, datetime.combine(today, time.max), sport)


def chat_coaches(db: DB, cfg: Config, today: date) -> list[coaches.Coach]:
    """The coaches the athlete can address: one per active sport, primary first."""
    return [chat_coach(db, cfg, today, s) for s in cfg.active_sports(today)]


def addressed(db: DB, cfg: Config, today: date, key: str | None) -> coaches.Coach:
    """The coach a message goes to: the one picked, if it coaches an active sport; else the primary sport's."""
    return next((c for c in chat_coaches(db, cfg, today) if c.key == key), chat_coaches(db, cfg, today)[0])


async def respond(db: DB, cfg: Config, snapshot: dict, today: date) -> int:
    """Answers the latest user message in the chat, by the coach it was sent to. Saves notes and memory right away;
    plan changes (in that coach's sport only) as a proposal."""
    ws = week_start(today)
    plan = db.active_plan(ws.isoformat())
    history = db.chat(HISTORY + 1)
    c = addressed(db, cfg, today, history[-1]["coach"] if history else None)
    two = len(cfg.active_sports(today)) > 1
    # Two sports: one hard-day limit for the week (the primary coach's), whichever coach changes it
    rules = coach.week_rules(db, cfg, ws, today) if two else c.rules(cfg.athlete_level, coach.weeks_with_coach(db, c.key, ws))
    kind = sports.ACTIVITY_KIND[c.sport]
    dates = open_dates(plan, today, any(r["date"] == today.isoformat() and r.get("sport", "ride") == kind
                                        for r in snapshot["recent_rides"]))
    notes = db.active_notes(today.isoformat())
    facts = db.memories()[-memory.MAX_FACTS:]

    messages = []
    for m in history[:-1]:
        text = m["content"]
        if m.get("kind") == "checkin":
            text = f"[Your daily check-in]\n{text}"
        if m["role"] == "assistant" and m["coach"] != c.key:
            text = f"[{'Your colleague' if m['coach'] in {x.key for x in chat_coaches(db, cfg, today)} else 'Earlier coach'}: " \
                   f"{coaches.name_of(m['coach'])}]\n{text}"
        if m["role"] == "user" and m["coach"] != c.key:
            text = f"[To {coaches.name_of(m['coach'])}]\n{text}"
        if m["role"] == "assistant" and m["proposal"]:
            text += f"\n[Proposed changes for {', '.join(d['date'] for d in m['proposal']['days'])}"
            text += ", applied]" if m["applied_plan_id"] else ", not applied]"
        messages.append({"role": m["role"], "content": text})
    snapshot = memory.for_coach(snapshot, c)
    if "athlete_history" in snapshot:  # memory goes once, with ids, below
        snapshot = snapshot | {"athlete_history": {k: v for k, v in snapshot["athlete_history"].items() if k != "memory"}}
    context = {
        "open_dates": dates,
        "your_sport": c.sport,
        "active_plan": {"your_sport_planned_by": coaches.name_of(memory.plan_coach(plan, c.sport)),
                        "week_focus": plan["payload"]["week_focus"],
                        "block_week": plan["payload"].get("block_week"),
                        "days": strip_cues(plan["payload"]["days"])} if plan else None,
        "coach_notes": notes,
        "memory": [{"id": f["id"], "since": f["created"][:10], "text": f["text"]} for f in facts],
        "snapshot": snapshot,
    }
    messages.append({"role": "user", "content": f"{history[-1]['content']}\n\n```json\n{json.dumps(context, indent=1)}\n```"})
    while messages and messages[0]["role"] != "user":  # the window may start with a reply
        messages.pop(0)

    result, warnings = await coach.run_validated(
        cfg, db, coach.system_prompt(c, rules, "chat" if two else None) + ADDENDUM, messages, chat_schema(c.sport),
        checker(plan, dates, cfg, snapshot, today, rules, c), purpose="chat", effort="medium", max_tokens=16000)

    now = datetime.now().isoformat(timespec="seconds")
    known = {n["id"] for n in notes}
    for note_id in result["remove_note_ids"]:
        if note_id in known:
            db.delete_note(note_id, now)
    held = {f["id"] for f in facts}
    for fact_id in result["forget_memory_ids"]:
        if fact_id in held:
            db.forget_memory(fact_id, now)
    remembered = [t for t in dict.fromkeys(t.strip() for t in result["remember"]) if t and t not in {f["text"] for f in facts}]
    for text in remembered:
        db.add_memory(now, c.key, text)
    saved = []
    for n in result["add_notes"]:
        try:
            start, end = date.fromisoformat(n["start"]), date.fromisoformat(n["end"])
        except ValueError:
            continue
        end = max(start, end)
        db.add_note(now, n["kind"], start.isoformat(), end.isoformat(), n["text"].strip())
        saved.append(f"{n['text'].strip()} ({_span(start, end)})")
    proposal = ({"plan_id": plan["id"], "sport": c.sport, "days": coach.tag(result["plan_changes"], c.sport, c.key)}
                if plan and result["plan_changes"] else None)
    return db.add_chat(now, "assistant", c.key, result["reply"], proposal, warnings if proposal else [], saved, remembered)


def _span(start: date, end: date) -> str:
    return start.strftime("%-d %b") if start == end else f"{start.strftime('%-d %b')}–{end.strftime('%-d %b')}"


def apply(db: DB, msg: dict, today: date) -> tuple[int | None, str]:
    """Saves an accepted proposal as a new active plan. Returns (plan id, message); id None if refused."""
    prop = msg.get("proposal")
    if not prop or msg["applied_plan_id"]:
        return None, "Nothing to apply."
    base = db.plan(prop["plan_id"])
    if not base or db.active_plan(base["week_start"])["id"] != base["id"]:
        return None, "The active plan changed since the coach proposed this. Ask again."
    if any(d["date"] < today.isoformat() for d in prop["days"]):
        return None, "This proposal changes days that are already past. Ask again."
    payload = copy.deepcopy(base["payload"])
    changed = prop["days"]
    payload["days"] = replace_days(payload["days"], coach.tag(copy.deepcopy(changed), prop.get("sport", "cycling"),
                                                               changed[0].get("coach") or msg["coach"]))
    payload["revised_from"] = base["id"]
    plan_id = db.save_plan(base["week_start"], datetime.now().isoformat(timespec="seconds"), base["model"],
                           payload, msg["warnings"], base["coach"])
    db.set_active_plan(base["week_start"], plan_id)
    db.mark_applied(msg["id"], plan_id)
    return plan_id, f"Plan updated: {len(changed)} day{'s' if len(changed) != 1 else ''} changed."
