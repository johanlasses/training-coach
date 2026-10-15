"""Apple Health numbers, read from the HA iOS companion app's sensors, as daily values and trends for the coach.

The app pushes HealthKit samples to HA as sensor states. Their timestamps are the push time, not the sample
time (home-assistant/iOS#5670), so a reading is filed under the day the phone reported it. A state the phone
didn't report today is yesterday's number, and isn't stored again. For HRV the first reading of the day is kept: Apple's
spot SDNN taken after a ride reads low, so the overnight/morning value is the useful one.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from math import exp, log
from statistics import mean

from . import ha
from .config import Config
from .db import DB

# metric: (label, unit we store)
METRICS = {
    "rhr": ("Resting heart rate", "bpm"),
    "hrv": ("Heart rate variability", "ms"),
    "sleep": ("Sleep", "h"),
    "weight": ("Weight", "kg"),
    "vo2max": ("VO2 max", "ml/kg/min"),
}
TO_HOURS = {"h": 1, "hr": 1, "hours": 1, "min": 1 / 60, "minutes": 1 / 60, "s": 1 / 3600}
TO_KG = {"kg": 1, "lb": 0.45359237, "lbs": 0.45359237}


def entities(cfg: Config) -> dict[str, str]:
    """Configured metric → entity id."""
    return {m: e for m in METRICS if (e := getattr(cfg, f"health_{m}_entity").strip())}


def normalise(metric: str, value: float, unit: str) -> float | None:
    unit = (unit or "").strip().lower()
    if metric == "sleep":
        return value * TO_HOURS[unit] if unit in TO_HOURS else None
    if metric == "weight":
        return value * TO_KG[unit] if unit in TO_KG else None
    return value


async def fetch(db: DB, cfg: Config, today: date) -> int:
    """Reads every configured sensor and stores today's value. Returns how many were stored."""
    n = 0
    for metric, entity in entities(cfg).items():
        s = await ha.state(entity)
        try:
            raw = float(s["state"]) if s else None
        except (TypeError, ValueError):  # "unknown", "unavailable"
            raw = None
        value = normalise(metric, raw, (s or {}).get("attributes", {}).get("unit_of_measurement", "")) if raw is not None else None
        if value is not None and value > 0 and changed_on(s) == today:
            db.set_health(today.isoformat(), metric, round(value, 2), METRICS[metric][1], keep_first=metric == "hrv")
            n += 1
    return n


def changed_on(s: dict) -> date | None:
    """Local date the phone last reported the sensor, even with an unchanged value (HA 2024.3+ last_reported;
    older HA only has last_updated/last_changed). None if HA didn't say."""
    for key in ("last_reported", "last_updated", "last_changed"):
        try:
            return datetime.fromisoformat(s[key]).astimezone().date()
        except (KeyError, TypeError, ValueError):
            continue
    return None


def candidates(states: list[dict]) -> list[dict]:
    """Sensors that look like Apple Health data, for the entity pickers on Profile."""
    out = []
    for s in states:
        eid, name = s.get("entity_id", ""), s.get("attributes", {}).get("friendly_name", "")
        if eid.startswith("sensor.") and ("health" in eid or "health" in name.lower()):
            out.append({"entity_id": eid, "label": f"{name or eid}: {s.get('state')} {s.get('attributes', {}).get('unit_of_measurement', '')}".strip()})
    return sorted(out, key=lambda c: c["entity_id"])


def summary(db: DB, today: date) -> dict:
    """Per metric: latest value, the last 7 days against a baseline of the 28 days before them, and how many
    days of data each holds. HRV is compared on a log scale (as a % change), the rest as plain differences."""
    rows = db.health((today - timedelta(days=34)).isoformat())
    week_from = (today - timedelta(days=6)).isoformat()
    out = {}
    for metric, (_, unit) in METRICS.items():
        vals = [(r["date"], r["value"]) for r in rows if r["metric"] == metric]
        if not vals:
            continue
        week = [v for d, v in vals if d >= week_from]
        base = [v for d, v in vals if d < week_from]
        item = {"unit": unit, "latest": vals[-1][1], "latest_date": vals[-1][0],
                "mean_7d": round(mean(week), 1) if week else None,
                "baseline_mean": round(mean(base), 1) if base else None,
                "days_7d": len(week), "days_baseline": len(base)}
        if week and base:
            if metric == "hrv":
                item["change_pct"] = round((exp(mean(map(log, week)) - mean(map(log, base))) - 1) * 100, 1)
            else:
                item["change"] = round(mean(week) - mean(base), 1)
        out[metric] = item
    return out
