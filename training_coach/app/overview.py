"""The status card at the top of the Overview: form, this week against the plan, fitness trend and the next event.
Plain numbers, no Claude call, so it's always current."""
from __future__ import annotations

import math
import re
from datetime import date, timedelta

from . import metrics, stats
from .coach import compliance
from .config import Config
from .db import DB
from .sync import ftp_at

TREND_DAYS = 28


def form_label(tsb: float) -> tuple[str, str]:
    """(state, the colour token for it). Below -25 matches where the planner starts reducing load."""
    if tsb < -25:
        return "High fatigue", "critical"
    if tsb < -10:
        return "Productive training", "good"
    if tsb <= 5:
        return "Neutral", "text-muted"
    if tsb <= 25:
        return "Fresh", "div-pos"
    return "Losing fitness", "warning"


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower().replace("×", "x")).strip()


def rides_for(session: dict, sessions: list[dict], acts: list[dict]) -> list[dict]:
    """The rides that were this session: those named after it (Zwift names the ride after the workout, and the
    athlete swaps days), else that day's rides that aren't named after another session."""
    named = [a for a in acts if _norm(session["title"]) and _norm(session["title"]) in _norm(a["name"] or "")]
    if named:
        return named
    others = [_norm(d["title"]) for d in sessions if d is not session and _norm(d["title"])]
    return [a for a in acts if a["start_date_local"][:10] == session["date"]
            and not any(t in _norm(a["name"] or "") for t in others)]


def week(db: DB, acts: list[dict], today: date) -> dict:
    ws = stats.week_start(today)
    plan = db.active_plan(ws.isoformat())
    done_tss = stats.window(acts, ws)["tss"]
    if not plan:
        return {"plan": False, "tss_done": done_tss, "rides": stats.window(acts, ws)["rides"]}
    week_acts = [a for a in acts if a["start_date_local"][:10] >= ws.isoformat()]
    days = plan["payload"]["days"]
    sessions = [d for d in days if d["kind"] != "rest"]
    status = {d["date"]: compliance(d, rides_for(d, sessions, week_acts)) for d in sessions}
    done = sum(1 for d in sessions if status[d["date"]] == "done")
    # Today's session is next until it's ridden
    nxt = next((d for d in sessions if d["date"] > today.isoformat()
                or (d["date"] == today.isoformat() and status[d["date"]] == "missed")), None)
    return {"plan": True, "plan_week": ws.isoformat(), "sessions": len(sessions), "done": done,
            "partial": sum(1 for d in sessions if status[d["date"]] == "partial"),
            "missed": sum(1 for d in sessions if d["date"] < today.isoformat() and status[d["date"]] == "missed"),
            "tss_done": done_tss, "tss_planned": sum(d["target_tss"] for d in days),
            "next": nxt, "next_is_today": bool(nxt and nxt["date"] == today.isoformat()),
            "focus": plan["payload"].get("week_focus", "")}


def trend(acts: list[dict], today: date, cfg: Config, fitness: list[dict]) -> dict:
    asc = sorted(acts, key=lambda a: a["start_date_local"])
    ftp_now, src_now = ftp_at(asc, today, cfg.ftp_override, cfg.ftp_tests)
    ftp_then, src_then = ftp_at(asc, today - timedelta(days=42), cfg.ftp_override, cfg.ftp_tests)
    ctl_now = fitness[-1]["ctl"] if fitness else 0
    ctl_then = fitness[-TREND_DAYS - 1]["ctl"] if len(fitness) > TREND_DAYS else (fitness[0]["ctl"] if fitness else 0)
    return {"ctl": round(ctl_now), "ctl_change": round(ctl_now - ctl_then, 1),
            "ctl_ramp": round((ctl_now - ctl_then) / (TREND_DAYS / 7), 1),
            "ftp": round(ftp_now), "ftp_source": src_now, "wkg": round(ftp_now / cfg.weight_kg, 2),
            # A test against an estimate isn't a trend, and an override never moves
            "ftp_change": round(ftp_now - ftp_then) if src_now == src_then and src_now in ("eftp", "test") else None}


def next_event(db: DB, today: date, ctl: float, acts: list[dict]) -> dict | None:
    """The nearest event the athlete told the coach about, with CTL on that day if the last 4 weeks' average
    daily load carries on: CTL is a 42-day average, so it closes in on that load instead of ramping forever."""
    events = [n for n in db.active_notes(today.isoformat()) if n["kind"] == "event" and n["start"] >= today.isoformat()]
    if not events:
        return None
    e = min(events, key=lambda n: n["start"])
    daily = metrics.daily_tss(acts)
    load = sum(daily.get(today - timedelta(days=k), 0) for k in range(TREND_DAYS)) / TREND_DAYS
    days = (date.fromisoformat(e["start"]) - today).days
    return {"text": e["text"], "date": e["start"], "days": days, "weeks": round(days / 7, 1),
            "ctl_projected": round(load + (ctl - load) * math.exp(-days / 42)) if days >= 7 else None}


def status(db: DB, cfg: Config, acts: list[dict], today: date, d: dict) -> dict:
    """`d` is stats.dashboard for the same acts."""
    t = trend(acts, today, cfg, d["fitness"])
    label, colour = form_label(d["tsb"])
    return {"form": label, "form_colour": colour, "tsb": round(d["tsb"]), "week": week(db, acts, today),
            "trend": t, "event": next_event(db, today, d["ctl"], acts)}
