"""What the coach knows beyond this week: profile changes, coach switches, earlier notes and lasting facts learned
in chat. Every coach gets it, so a new coach knows who came before and what happened under them."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from . import coaches, sports
from .config import LEVELS, Config
from .db import DB
from .stats import week_start

# Profile fields whose changes are logged, with their label
COACH_FIELDS = {"cycling_coach": "cycling", "running_coach": "running"}
TRACKED = {
    "goals": "Goals", "cycling_coach": "Cycling coach", "running_coach": "Running coach", "primary_sport": "Primary sport",
    "cycling_active": "Cycling", "cycling_until": "Cycling active until", "running_active": "Running",
    "running_until": "Running active until", "run_lthr": "Running threshold HR", "threshold_pace": "Threshold pace",
    "athlete_level": "Level", "sessions_per_week": "Sessions per week",
    "weight_kg": "Weight", "ftp_override": "FTP override", "lthr": "Threshold HR", "available_days": "Training days",
    "max_weekday_minutes": "Max minutes on weekdays", "max_weekend_minutes": "Max minutes on weekends",
}
MAX_FACTS = 60  # newest facts sent to the coach


def tracked(cfg: Config) -> dict:
    return {k: getattr(cfg, k) for k in TRACKED}


def _show(name: str, v) -> str:
    if name == "goals":
        return f"“{v[:300]}{'…' if len(v) > 300 else ''}”" if v else "none"
    if name in COACH_FIELDS:
        return coaches.name_of(v)
    if name in ("cycling_active", "running_active"):
        return "active" if v else "on hold"
    if name in ("cycling_until", "running_until"):
        return v or "no end date"
    if name == "primary_sport":
        return sports.SPORTS.get(v, v)
    if name == "threshold_pace":
        return f"{v // 60}:{v % 60:02d}/km" if v else "estimated"
    if name == "run_lthr" and not v:
        return "same as cycling"
    if name == "athlete_level":
        return LEVELS.get(v, (v,))[0]
    if name == "available_days":
        return ", ".join(d.capitalize() for d in v) or "none"
    unit = {"weight_kg": " kg", "ftp_override": " W", "lthr": " bpm", "run_lthr": " bpm"}.get(name, "")
    return "auto" if name == "ftp_override" and not v else f"{v:g}{unit}" if isinstance(v, float) else f"{v}{unit}"


def log_changes(db: DB, before: dict, cfg: Config, ts: str) -> list[str]:
    """Logs each tracked field that differs from `before`. Returns the texts."""
    out = []
    for name, label in TRACKED.items():
        old, new = before[name], getattr(cfg, name)
        if old == new:
            continue
        text = f"{label}: {_show(name, old)} → {_show(name, new)}"
        if name in COACH_FIELDS:
            db.log(ts, "coach", text, {"from": old, "to": new, "sport": COACH_FIELDS[name]})
        else:
            db.log(ts, "profile", text, {"field": name, "old": old, "new": new})
        out.append(text)
    return out


def plan_coach(plan: dict | None, sport: str = "cycling") -> str | None:
    """Who planned a sport's sessions in a plan: the days' coach; plans from before 0.11.0 are all cycling, by the
    plan's coach."""
    if not plan:
        return None
    own = coaches.COACHES.get(plan["coach"] or "")
    if own and own.sport == sport:  # the plan's coach; a chat revision by a new coach only stamps the days it changed
        return plan["coach"]
    keys = [d["coach"] for d in plan["payload"]["days"] if d.get("coach") and sports.day_sport(d) == sport]
    if keys:
        return keys[0]
    return plan["coach"] if sport == "cycling" and not any(d.get("sport") for d in plan["payload"]["days"]) else None


def last_switch(db: DB, when: datetime, sport: str) -> dict | None:
    """The latest coach switch for a sport on Profile (logs from before 0.11.0 have no sport: cycling)."""
    entries = [e for e in db.log_entries(limit=200) if e["kind"] == "coach" and e["ts"] <= when.isoformat(timespec="seconds")
               and e["data"].get("sport", "cycling") == sport]
    return entries[0] if entries else None


def coach_in_charge(db: DB, cfg: Config, when: datetime, sport: str | None = None) -> coaches.Coach:
    """A sport's coach (default: the primary sport's): the coach of that week's plan for it, unless the athlete
    switched that sport's coach on Profile after the plan was made. The latest decision wins: a plan made after the
    switch, with any coach, takes over again."""
    sport = sport or cfg.active_sports(when.date())[0]
    plan = db.active_plan(week_start(when.date()).isoformat())
    planned = plan_coach(plan, sport)
    switch = last_switch(db, when, sport)
    if planned and not (switch and switch["ts"] > made(db, plan)):
        return coaches.get(planned, sport)
    return coaches.get(switch["data"]["to"] if switch else cfg.coach_key(sport), sport)


def made(db: DB, plan: dict) -> str:
    """When the week's plan was decided: a chat revision (revised_from) is the same decision as its original."""
    while (prev := plan["payload"].get("revised_from")) and (base := db.plan(prev)):
        plan = base
    return plan["created_at"]


def for_coach(snapshot: dict, c: coaches.Coach) -> dict:
    """The snapshot with the requesting coach's runs marked "you", so a comparison plan or a takeover reads right."""
    if "athlete_history" not in snapshot:
        return snapshot
    h = snapshot["athlete_history"]
    return snapshot | {"athlete_history": h | {"coaches": [r | {"you": r["coach"] == c.name} for r in h["coaches"]]}}


def _past_note(n: dict) -> dict:
    """A note as it really went: removed early means it ended then; removed before it began, it never happened."""
    out = {k: n[k] for k in ("kind", "start", "end", "text")}
    if n["removed"]:
        removed = n["removed"][:10]
        out |= {"cancelled": True} if removed < n["start"] else {"end": min(n["end"], removed), "removed": removed}
    return out


def coach_timeline(db: DB, cfg: Config, when: datetime, weeks: int = 52) -> list[dict]:
    """Who coached the athlete, oldest first: runs of weeks whose active plan came from one coach, per sport (running
    entries say so). A switch on Profile that no plan reflects yet starts a run on the switch date. The current run
    has no end."""
    out = sport_timeline(db, cfg, when, "cycling", weeks)
    running = sport_timeline(db, cfg, when, "running", weeks)
    if running and (cfg.sport_on("running", when.date()) or any(r["weeks_planned"] for r in running)):
        out = sorted(out + [r | {"sport": "running"} for r in running], key=lambda r: r["from"])
    return out


def sport_timeline(db: DB, cfg: Config, when: datetime, sport: str, weeks: int = 52) -> list[dict]:
    this_week = week_start(when.date())
    runs = []
    for n in range(weeks, -1, -1):
        ws = this_week - timedelta(weeks=n)
        plan = db.active_plan(ws.isoformat())
        if not plan:
            continue
        key = plan_coach(plan, sport)
        if key is None and (sport == "running" or any(d.get("sport") for d in plan["payload"]["days"])):
            continue  # no sessions of this sport that week (a None key on an old plan is a coach from before coaches)
        if runs and runs[-1]["key"] == key:
            runs[-1].update(to=ws + timedelta(days=6), weeks=runs[-1]["weeks"] + 1)
        else:
            runs.append({"key": key, "from": ws, "to": ws + timedelta(days=6), "weeks": 1})
    current = coach_in_charge(db, cfg, when, sport)
    switch = last_switch(db, when, sport)
    if switch and (not runs or runs[-1]["key"] != current.key):
        since = date.fromisoformat(switch["ts"][:10])
        if runs:
            runs[-1]["to"] = min(runs[-1]["to"], since - timedelta(days=1))
            if runs[-1]["to"] < runs[-1]["from"]:  # switched before that plan's week began: it never ran
                runs.pop()
        runs.append({"key": current.key, "from": since, "to": None, "weeks": 0})
    if runs and runs[-1]["key"] == current.key:
        runs[-1]["to"] = None
    return [{"coach": coaches.name_of(r["key"]),
             "approach": coaches.COACHES[r["key"]].summary if r["key"] in coaches.COACHES else None,
             "from": r["from"].isoformat(), "to": r["to"].isoformat() if r["to"] else None, "weeks_planned": r["weeks"]}
            for r in runs]


def history(db: DB, cfg: Config, when: datetime, memory_ids: bool = False) -> dict:
    """The athlete's story for the prompts (and Profile): the last year, plus every fact still held."""
    today = when.date()
    year_ago = (today - timedelta(days=365)).isoformat()
    return {
        "coaches": coach_timeline(db, cfg, when),
        "changes": [{"date": e["ts"][:10], "change": e["text"]} for e in db.log_entries(year_ago)],
        "past_notes": [_past_note(n) for n in db.past_notes(today.isoformat(), year_ago)],
        "ftp_results": [{"date": t["date"], "ftp_w": t["ftp"], "protocol": t["protocol"]}
                        for t in sorted(cfg.ftp_tests, key=lambda t: t["date"])],
        "memory": [{"id": m["id"], "since": m["created"][:10], "text": m["text"]} if memory_ids else m["text"]
                   for m in db.memories()[-MAX_FACTS:]],
    }
