"""The status card at the top of the Overview: form, this week against the plan, fitness trend and the next event.
Plain numbers, no Claude call, so it's always current."""
from __future__ import annotations

import math
import re
from datetime import date, timedelta

from . import health, metrics, stats
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


def day_statuses(plan: dict, acts: list[dict], upto: str) -> list[tuple[dict, str]]:
    """(plan day, compliance status) for the plan's days up to and including `upto`."""
    days = plan["payload"]["days"]
    sessions = [d for d in days if d["kind"] != "rest"]
    week_acts = [a for a in acts if a["start_date_local"][:10] >= plan["week_start"]]
    return [(d, compliance(d, rides_for(d, sessions, week_acts) if d["kind"] != "rest"
                           else [a for a in week_acts if a["start_date_local"][:10] == d["date"]]))
            for d in days if d["date"] <= upto]


def today_session(db: DB, today: date) -> dict | None:
    plan = db.active_plan(stats.week_start(today).isoformat())
    if not plan:
        return None
    return next((d for d in plan["payload"]["days"] if d["date"] == today.isoformat() and d["kind"] != "rest"), None)


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
            # Today's session counts once today has a ride, so the morning doesn't look behind
            "tss_planned_to_date": sum(d["target_tss"] for d in days if d["date"] < today.isoformat() or (
                d["date"] == today.isoformat() and any(a["start_date_local"][:10] == d["date"] for a in week_acts))),
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


# --- readiness gauges -------------------------------------------------------
# Small semicircle gauges for the status card. Geometry and bands are worked out here; _macros.html only draws them.
# Each band is (from, to, colour token). Form's bands are form_label's; the health ones follow health.READINESS.
R = health.READINESS
FORM_BANDS = [(-40, -25, "critical"), (-25, -10, "good"), (-10, 5, "text-muted"), (5, 25, "div-pos"), (25, 30, "warning")]
HRV_BANDS = [(-20, R["hrv_drop_pct"], "critical"), (R["hrv_drop_pct"], -5, "warning"), (-5, 10, "good"), (10, 20, "text-muted")]
RHR_BANDS = [(-6, 2, "good"), (2, R["rhr_rise_bpm"], "warning"), (R["rhr_rise_bpm"], 8, "critical")]
SLEEP_BANDS = [(4, R["sleep_min_h"], "critical"), (R["sleep_min_h"], 7, "warning"), (7, 9, "good")]


def _point(t: float) -> tuple[float, float]:
    """A point on the arc: t=0 is the left end, t=1 the right; centre (50, 50), radius 40."""
    return round(50 - 40 * math.cos(math.pi * t), 2), round(50 - 40 * math.sin(math.pi * t), 2)


def _arc(t1: float, t2: float) -> str:
    (x1, y1), (x2, y2) = _point(t1), _point(t2)
    return f"M{x1} {y1} A40 40 0 0 1 {x2} {y2}"


def gauge(value: float, lo: float, hi: float, bands: list[tuple], text: str, label: str, sub: str,
          fill: bool = False, tick: float | None = None, colour: str | None = None) -> dict:
    """Banded gauge with a marker at `value`, or (fill) a progress arc from `lo` with an optional tick. `colour`
    overrides the band lookup when a rule elsewhere decides (form_label, the readiness signal)."""
    t = lambda v: (min(max(v, lo), hi) - lo) / (hi - lo)  # noqa: E731
    colour = colour or next((c for a, b, c in bands if a <= value < b), bands[-1][2] if value >= hi else bands[0][2])
    g = {"text": text, "label": label, "sub": sub, "colour": colour, "track": _arc(0, 1),
         "bands": [] if fill else [{"d": _arc(t(a), t(b)), "colour": c} for a, b, c in bands],
         "marker": None if fill else _point(t(value)), "fill": _arc(0, t(value)) if fill and t(value) > 0 else None}
    if tick is not None:
        (x1, y1), tt = _point(t(tick)), math.pi * t(tick)
        g["tick"] = (x1, y1, round(50 - 48 * math.cos(tt), 2), round(50 - 48 * math.sin(tt), 2))
    return g


def spark(values: list[float], top: float = 8, bottom: float = 38) -> str:
    """SVG polyline points for a small trend line inside the gauge's 100-wide box."""
    if len(values) < 2:
        return ""
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1
    step = 92 / (len(values) - 1)
    return " ".join(f"{4 + i * step:.1f},{bottom - (v - lo) / span * (bottom - top):.1f}" for i, v in enumerate(values))


def fitness_gauge(t: dict, fitness: list[dict], days: int = 42) -> dict:
    """Not an arc: CTL over the last six weeks as a small line, with today's CTL and the 4-week change."""
    change = t["ctl_change"]
    return {"label": "Fitness", "text": str(t["ctl"]), "line": spark([f["ctl"] for f in fitness[-days:]]),
            "colour": "good" if change > 0 else "critical" if change < -2 else "text-muted",
            "sub": f"FTP {t['ftp']} · {'↑' if round(change) > 0 else '↓' if round(change) < 0 else '→'}{abs(round(change))}"}


def first_sentence(text: str, limit: int = 140) -> str:
    """The session description's first sentence, for the Overview's next-session line."""
    text = " ".join((text or "").split())
    m = re.search(r"[.!?](\s|$)", text)
    s = text[:m.end()].strip() if m else text
    return s if len(s) <= limit else s[:limit - 1].rsplit(" ", 1)[0] + "…"


def gauges(st: dict, ready: dict | None, fitness: list[dict] | None = None) -> list[dict]:
    """The status card's gauges: form, the week's TSS and sessions against the plan, fitness, and HRV and sleep (or
    RHR) when they're trustworthy."""
    out = [gauge(st["tsb"], -40, 30, FORM_BANDS, f"{st['tsb']:+d}", "Form", st["form"], colour=st["form_colour"])]
    w = st["week"]
    if w["plan"] and w["tss_planned"]:
        pct = round(w["tss_done"] / w["tss_planned"] * 100)
        out.append(gauge(min(pct, 120), 0, 120, [(0, 120, "series-1")], f"{pct}%", "Week",
                         f"TSS {w['tss_done']}/{w['tss_planned']}", fill=True,
                         tick=w["tss_planned_to_date"] / w["tss_planned"] * 100))
    if w["plan"] and w["sessions"]:
        sub = f"{w['missed']} missed" if w["missed"] else f"{w['partial']} partly" if w["partial"] else "on track"
        out.append(gauge(w["done"], 0, w["sessions"], [(0, w["sessions"], "good")], f"{w['done']}/{w['sessions']}",
                         "Sessions", sub, fill=True))
    if fitness:
        out.append(fitness_gauge(st["trend"], fitness))
    health_gauges = []
    if h := (ready or {}).get("hrv"):
        # A sharp HRV rise under heavy load isn't freshness (the planner's rule): amber, not grey or green
        high_under_load = h["change_pct"] > 10 and st["tsb"] < -25
        health_gauges.append((h["signal"], gauge(
            h["change_pct"], -20, 20, HRV_BANDS, f"{h['change_pct']:+.0f}%", "HRV",
            "high under load" if high_under_load else "vs baseline",
            colour="critical" if h["signal"] else "warning" if high_under_load else None)))
    if s := (ready or {}).get("sleep"):
        health_gauges.append((s["signal"], gauge(s["mean_7d"], 4, 9, SLEEP_BANDS, f"{s['mean_7d']:.1f} h", "Sleep",
                                                 "7-day avg", colour="critical" if s["signal"] else None)))
    if r := (ready or {}).get("rhr"):
        health_gauges.append((r["signal"], gauge(r["change"], -6, 8, RHR_BANDS, f"{r['change']:+.0f}", "Resting HR",
                                                 "bpm vs base", colour="critical" if r["signal"] else None)))
    # Two slots: whatever is off goes first, so a signal the planner acted on is never hidden; else HRV, sleep, RHR
    return out + [g for _, g in sorted(health_gauges, key=lambda x: not x[0])[:2]]
