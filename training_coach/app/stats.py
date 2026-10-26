"""Aggregates for the dashboard and the coaching snapshot."""
from __future__ import annotations

from datetime import date, timedelta

from . import coaches, metrics, season, sports
from .config import Config, level_for_wkg
from .sync import ftp_at, threshold_pace_at


def _day(a: dict) -> date:
    return date.fromisoformat(a["start_date_local"][:10])


def window(acts: list[dict], start: date, end: date | None = None) -> dict:
    """Ride volume (rides, hours, km: a run's km aren't a ride's), the run and walk volume next to it, and the TSS of
    everything: fatigue is shared."""
    end = end or date.max
    every = [a for a in acts if start <= _day(a) <= end]
    sel = sports.rides(every)
    runs = sports.runs(every)
    walks = [a for a in every if sports.kind(a["sport_type"]) == "walk"]
    indoor = [a for a in sel if a["trainer"] or a["sport_type"] == "VirtualRide"]
    return {
        "rides": len(sel),
        "hours": round(sum(a["moving_time"] or 0 for a in sel) / 3600, 1),
        "km": round(sum(a["distance"] or 0 for a in sel) / 1000),
        "elevation": round(sum(a["elevation_gain"] or 0 for a in sel)),
        "tss": round(sum(a["tss"] or 0 for a in every)),
        "indoor_rides": len(indoor),
        "indoor_hours": round(sum(a["moving_time"] or 0 for a in indoor) / 3600, 1),
        "runs": len(runs),
        "run_hours": round(sum(a["moving_time"] or 0 for a in runs) / 3600, 1),
        "run_km": round(sum(a["distance"] or 0 for a in runs) / 1000, 1),
        "walk_hours": round(sum(a["moving_time"] or 0 for a in walks) / 3600, 1),
    }


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def weekly(acts: list[dict], today: date, weeks: int = 12) -> list[dict]:
    ws = week_start(today)
    out = []
    for i in range(weeks - 1, -1, -1):
        s = ws - timedelta(weeks=i)
        w = window(acts, s, s + timedelta(days=6))
        out.append({"week": s.isoformat(), **w})
    return out


def fitness(acts: list[dict], today: date, days: int = 90) -> list[dict]:
    return metrics.fitness_series(metrics.daily_tss(acts), today - timedelta(days=days - 1), today)


def power_curve(acts: list[dict], today: date, days: int) -> dict[int, float]:
    lo = today - timedelta(days=days)
    return metrics.best_curve([a["mmp"] for a in sports.rides(acts) if a.get("mmp") and _day(a) >= lo])


def zone_distribution(acts: list[dict], today: date, days: int = 28) -> list[dict]:
    lo = today - timedelta(days=days)
    totals = [0] * len(metrics.ZONES)
    for a in sports.rides(acts):
        if a.get("power_hist") and _day(a) >= lo:
            for i, s in enumerate(metrics.zone_seconds(a["power_hist"], a["ftp_used"])):
                totals[i] += s
    total = sum(totals) or 1
    return [{"zone": z, "hours": round(s / 3600, 1), "pct": round(s / total * 100)}
            for (z, _), s in zip(metrics.ZONES, totals)]


def ftp_trend(acts: list[dict], today: date, cfg: Config, weeks: int = 12) -> list[dict]:
    asc = sorted(acts, key=lambda a: a["start_date_local"])
    return [{"week": (today - timedelta(weeks=i)).isoformat(),
             "ftp": ftp_at(asc, today - timedelta(weeks=i), 0)[0]}
            for i in range(weeks, -1, -2)] if not cfg.ftp_override else []  # eFTP only: compare it with tests


def dashboard(acts: list[dict], today: date, cfg: Config) -> dict:
    ftp, src = ftp_at(acts, today, cfg.ftp_override, cfg.ftp_tests)
    fit = fitness(acts, today)
    cur = fit[-1] if fit else {"ctl": 0, "atl": 0, "tsb": 0}
    return {
        "ftp": round(ftp), "ftp_source": src, "wkg": round(ftp / cfg.weight_kg, 2),
        "ctl": cur["ctl"], "atl": cur["atl"], "tsb": cur["tsb"],
        "this_week": window(acts, week_start(today)),
        "last7": window(acts, today - timedelta(days=6)),
        "last30": window(acts, today - timedelta(days=29)),
        "ytd": window(acts, date(today.year, 1, 1)),
        "weekly": weekly(acts, today),
        "fitness": fit,
        "curve90": power_curve(acts, today, 90),
        "curve365": power_curve(acts, today, 365),
        "zones28": zone_distribution(acts, today),
    }


def maximal_effort(a: dict) -> bool:
    """A test-like effort inside a ride: 20 min at ≥95%, 8 min at ≥105% or 5 min at ≥115% of the FTP the ride
    was scored with."""
    mmp, ftp = a.get("mmp") or {}, a.get("ftp_used") or 0
    return bool(ftp) and ((mmp.get(1200) or 0) >= 0.95 * ftp or (mmp.get(480) or 0) >= 1.05 * ftp
                          or (mmp.get(300) or 0) >= 1.15 * ftp)


def snapshot(acts: list[dict], today: date, cfg: Config) -> dict:
    """Compact, factual state of the athlete for the LLM. No raw streams."""
    d = dashboard(acts, today, cfg)
    curve = d["curve90"]
    cp = metrics.critical_power(curve)
    recent = [a for a in acts if _day(a) >= today - timedelta(days=27)]
    hours, sunrise, sunset = season.daylight(today)
    return {
        "today": today.isoformat(),
        "weekday": today.strftime("%A"),
        "athlete": {"weight_kg": cfg.weight_kg, "ftp_w": d["ftp"], "ftp_source": d["ftp_source"],
                    "w_per_kg": d["wkg"], "lthr": cfg.lthr,
                    "critical_power_w": cp[0] if cp else None, "w_prime_j": cp[1] if cp else None,
                    "goals": cfg.goals.strip() or "Not stated; assume general fitness and FTP gain.",
                    "self_rated_level": cfg.athlete_level,
                    "level_suggested_by_w_per_kg": level_for_wkg(d["wkg"]),
                    "target_sessions_per_week": cfg.sessions_per_week},
        "load": {"ctl": d["ctl"], "atl": d["atl"], "tsb": d["tsb"],
                 "ctl_4_weeks_ago": d["fitness"][-29]["ctl"] if len(d["fitness"]) >= 29 else None,
                 "ctl_ramp_per_week": round((d["ctl"] - d["fitness"][-8]["ctl"]), 1) if len(d["fitness"]) >= 8 else None},
        "weeks": [{k: w[k] for k in ("week", "rides", "hours", "km", "tss", "indoor_hours")}
                  | ({k: w[k] for k in ("runs", "run_hours", "run_km", "walk_hours")} if w["runs"] or w["walk_hours"] else {})
                  for w in d["weekly"]],
        "power_curve_90d_w": curve,
        "power_curve_365d_w": d["curve365"],
        "ftp_trend": ftp_trend(acts, today, cfg),
        "zones_last_28d": d["zones28"],
        "recent_rides": [{
            "date": a["start_date_local"][:10], "name": a["name"], "type": a["sport_type"],
            "sport": sports.kind(a["sport_type"]),
            "indoor": bool(a["trainer"] or a["sport_type"] == "VirtualRide"),
            "minutes": round((a["moving_time"] or 0) / 60), "km": round((a["distance"] or 0) / 1000, 1),
            "elev_m": round(a["elevation_gain"] or 0), "avg_w": a["avg_watts"], "np_w": round(a["np"]) if a.get("np") else None,
            "if": a["intensity"], "tss": a["tss"], "tss_source": a["tss_source"],
            "avg_hr": a["avg_hr"], "decoupling_pct": a.get("decoupling"),
            "best_5min_w": (a.get("mmp") or {}).get(300), "best_20min_w": (a.get("mmp") or {}).get(1200),
            "maximal_effort": maximal_effort(a),
        } | ({"pace_per_km": pace(a)} if not sports.is_ride(a) else {})
            for a in sorted(recent, key=lambda a: a["start_date_local"])],
        "season": {"phase": season.phase(today), "description": season.PHASE_TEXT[season.phase(today)],
                   "daylight_hours": hours, "sunrise": sunrise, "sunset": sunset},
        "availability": {wd: cfg.max_minutes(i) for i, wd in enumerate(
            ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"])},
        "sports": {"primary": cfg.primary_sport, "active": cfg.active_sports(today)},
        "running": running(acts, today, cfg),
    }


def pace(a: dict) -> str | None:
    """"5:12/km" from the average speed."""
    v = a.get("avg_speed")
    if not v or v <= 0:
        return None
    secs = round(1000 / v)
    return f"{secs // 60}:{secs % 60:02d}/km"


def fmt_pace(secs: float | None) -> str | None:
    return f"{int(secs) // 60}:{int(secs) % 60:02d}/km" if secs else None


RUN_WEEKS = 8


def run_weeks(acts: list[dict], today: date, weeks: int = RUN_WEEKS) -> list[dict]:
    """Run minutes and km per week, oldest first, ending with the current week."""
    ws = week_start(today)
    out = []
    for i in range(weeks - 1, -1, -1):
        s = ws - timedelta(weeks=i)
        sel = [a for a in sports.runs(acts) if s <= _day(a) <= s + timedelta(days=6)]
        out.append({"week": s.isoformat(), "runs": len(sel), "minutes": round(sum(a["moving_time"] or 0 for a in sel) / 60),
                    "km": round(sum(a["distance"] or 0 for a in sel) / 1000, 1)})
    return out


def returning(weeks: list[dict]) -> bool:
    """In the first 2 weeks back after 4+ weeks without a run: no run in the last 4 completed weeks, or last week was
    the first one back after 4 empty ones."""
    done = [w["runs"] for w in weeks[:-1]]  # completed weeks, oldest first
    return (len(done) >= 4 and not any(done[-4:])) or (len(done) >= 5 and done[-1] > 0 and not any(done[-5:-1]))


def running(acts: list[dict], today: date, cfg: Config) -> dict:
    """The run history and limits the durability guard (coach.run_errors), the running coach and run feedback work
    from. Run volume is judged from running alone: cycling fitness doesn't make tendons and bones ready for impact."""
    runs = sports.runs(acts)
    last = max((_day(a) for a in runs), default=None)
    weeks = run_weeks(acts, today)
    done = weeks[:-1]  # the current week is still running
    pace_s = threshold_pace_at(acts, today, cfg)
    avg_min = round(sum(w["minutes"] for w in done[-4:]) / 4)
    avg_km = round(sum(w["km"] for w in done[-4:]) / 4, 1)
    # Return-to-run floor from running history, not cycling level: ~3 × 20 min run/walk after 12 weeks without a run
    ran_12w = any(week_start(today) - timedelta(weeks=12) <= _day(a) < week_start(today) for a in runs)
    longest = max(((a["moving_time"] or 0) / 60 for a in runs if today - timedelta(days=30) <= _day(a) < today), default=0)
    run_coach = coaches.get(cfg.running_coach, "running")
    return {
        "weeks": weeks,
        "avg_minutes_last_4_weeks": avg_min, "avg_km_last_4_weeks": avg_km,
        "cap_minutes": round(max(avg_min * 1.1, 90 if ran_12w else 60)),
        "longest_run_min_30d": round(longest), "longest_run_cap_min": round(max(longest * 1.1, 30)),
        "returning": returning(weeks),
        # 3 hard runs only with the volume for double threshold (~75 km a week), else at most 2
        "max_hard_runs": min(run_coach.rules(cfg.athlete_level)["max_hard_sessions"], 3 if avg_km >= 75 else 2),
        "last_run": last.isoformat() if last else None,
        "weeks_since_last_run": (today - last).days // 7 if last else None,
        "threshold_pace": fmt_pace(pace_s), "threshold_pace_source": "profile" if cfg.threshold_pace else "estimated" if pace_s else None,
        "threshold_hr": cfg.run_threshold_hr, "threshold_hr_source": "profile" if cfg.run_lthr else "cycling LTHR + 5",
        "dark_runs_ok": cfg.run_in_dark,
    }
