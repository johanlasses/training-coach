"""Health numbers (resting HR, HRV, sleep, weight) as daily values and trends for the coach, from one of two sources.

Garmin (fetch_garmin): daily values straight from Garmin Connect, per calendar date. HRV is the overnight average
(rMSSD), which is why switching source clears the history: it's a different measure from Apple's SDNN.

Apple Health (fetch): read from the HA iOS companion app's sensors. The app pushes HealthKit samples to HA as sensor states. Their timestamps are the push time, not the sample
time (home-assistant/iOS#5670), so a reading is filed under the day the phone reported it. A state the phone
didn't report today is yesterday's number, and isn't stored again. For HRV the first reading of the day is kept: Apple's
spot SDNN taken after a ride reads low, so the overnight/morning value is the useful one.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from math import exp, log
from statistics import mean

from . import ha
from .config import Config
from .db import DB

logger = logging.getLogger(__name__)

# metric: (label, unit we store)
METRICS = {
    "rhr": ("Resting heart rate", "bpm"),
    "hrv": ("Heart rate variability", "ms"),
    "sleep": ("Sleep", "h"),
    "weight": ("Weight", "kg"),
    "vo2max": ("VO2 max", "ml/kg/min"),
}
# How the HA iOS app names each metric: sensor.<device>_<key>, e.g. sensor.johan_iphone_resting_heart_rate
SENSOR_KEYS = {"rhr": "resting_heart_rate", "hrv": "heart_rate_variability", "sleep": "sleep_duration",
               "weight": "weight", "vo2max": "vo2_max"}
# Readiness signals from the trends in summary(). The planner's rule text (coach.HEALTH_RULES), the daily check-in and the
# Overview gauges all read these, so they never disagree.
READINESS = {"min_days_7d": 5, "min_days_baseline": 14, "hrv_drop_pct": -10, "rhr_rise_bpm": 4, "sleep_min_h": 6.5}
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


def enabled(cfg: Config) -> bool:
    """Health data is set up: Garmin with a key, or Apple Health with at least one sensor picked."""
    return bool(cfg.garmin_api_key) if cfg.health_source == "garmin" else bool(entities(cfg))


GARMIN_BACKFILL_DAYS = 35  # the 7 days plus the 28-day baseline: Garmin keeps history, so trends work from day one
GARMIN_RECHECK_DAYS = 3    # the watch syncs late at times (sleep and HRV land after the morning upload)


async def fetch_garmin(db: DB, garmin, today: date) -> int:
    """Stores Garmin's daily RHR, HRV (overnight average), sleep and weight. The first read backfills the baseline;
    later ones re-read from the last stored day (at least the last few days), so days the add-on was down get filled
    in too. Returns how many values were stored."""
    oldest = today - timedelta(days=GARMIN_BACKFILL_DAYS - 1)
    rows = db.health(oldest.isoformat())
    first_time = not rows
    latest = date.fromisoformat(max(r["date"] for r in rows)) if rows else oldest
    first = oldest if first_time else max(oldest, min(today - timedelta(days=GARMIN_RECHECK_DAYS - 1), latest))
    days = await garmin.health_days(first, today)
    n = 0
    for day, values in days.items():
        for metric, value in values.items():
            if metric in METRICS and value > 0:
                db.set_health(day, metric, value, METRICS[metric][1])
                n += 1
    logger.info("Garmin health: stored %d values for %d days%s", n, len(days), " (backfill)" if first_time else "")
    return n


async def fetch(db: DB, cfg: Config, today: date) -> int:
    """Reads every configured sensor and stores today's value. Returns how many were stored."""
    stored, skipped = [], []
    for metric, entity in entities(cfg).items():
        s = await ha.state(entity)
        try:
            raw = float(s["state"]) if s else None
        except (TypeError, ValueError):  # "unknown", "unavailable"
            raw = None
        value = normalise(metric, raw, (s or {}).get("attributes", {}).get("unit_of_measurement", "")) if raw is not None else None
        if value is not None and value > 0 and changed_on(s) == today:
            db.set_health(today.isoformat(), metric, round(value, 2), METRICS[metric][1], keep_first=metric == "hrv")
            stored.append(f"{metric} {value:g}")
        else:
            unit = (s or {}).get("attributes", {}).get("unit_of_measurement", "")
            skipped.append(f"{metric} (" + ("no value" if raw is None or (value is not None and value <= 0) else
                                            f"unknown unit {unit!r}" if value is None else "not reported today") + ")")
    if stored or skipped:
        logger.info("Apple Health: stored %s%s", ", ".join(stored) or "nothing", f"; skipped {', '.join(skipped)}" if skipped else "")
    return len(stored)


def changed_on(s: dict) -> date | None:
    """Local date the phone last reported the sensor, even with an unchanged value (HA 2024.3+ last_reported;
    older HA only has last_updated/last_changed). None if HA didn't say."""
    for key in ("last_reported", "last_updated", "last_changed"):
        try:
            return datetime.fromisoformat(s[key]).astimezone().date()
        except (KeyError, TypeError, ValueError):
            continue
    return None


def metric_of(entity_id: str) -> str | None:
    return next((m for m, key in SENSOR_KEYS.items() if entity_id.endswith("_" + key)), None)


def phone_sensors(phones: list[str]) -> set[str]:
    """The Apple Health entity ids the iOS app gives these phones (exact: a phone "iphone" mustn't match iphone_2's)."""
    return {f"sensor.{p}_{key}" for p in phones for key in SENSOR_KEYS.values()}


def candidates(states: list[dict], phones: list[str] | None = None) -> list[dict]:
    """Sensors that look like Apple Health data, for the entity pickers on Profile: the iOS app's own names, or
    "health" in the id or name (Health Auto Export and the like). With `phones`, only those devices' sensors."""
    out = []
    for s in states:
        eid, name = s.get("entity_id", ""), s.get("attributes", {}).get("friendly_name", "")
        if phones is not None and eid not in phone_sensors(phones):
            continue
        if eid.startswith("sensor.") and (metric_of(eid) or "health" in eid or "health" in name.lower()):
            unit = s.get("attributes", {}).get("unit_of_measurement", "")
            out.append({"entity_id": eid, "metric": metric_of(eid), "unit": unit,
                        "available": s.get("state") not in ("unknown", "unavailable"),
                        "label": f"{name or eid}: {s.get('state')} {unit}".strip()})
    return sorted(out, key=lambda c: c["entity_id"])


def detect(options: list[dict]) -> dict[str, str]:
    """Metric → sensor to pre-fill: reporting now, in a unit we can store, and from the device that reports the most
    metrics (the phone), so a printer's filament weight never wins."""
    usable = [o for o in options if o["metric"] and o["available"] and normalise(o["metric"], 1, o["unit"]) is not None]
    prefix = {o["entity_id"]: o["entity_id"].removesuffix("_" + SENSOR_KEYS[o["metric"]]) for o in usable}
    per_device = {p: list(prefix.values()).count(p) for p in prefix.values()}
    out: dict[str, str] = {}
    for o in sorted(usable, key=lambda o: (-per_device[prefix[o["entity_id"]]], o["entity_id"])):
        out.setdefault(o["metric"], o["entity_id"])
    return out


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


def trusted(item: dict) -> bool:
    """Enough days in the week and the baseline to read a trend from."""
    return item["days_7d"] >= READINESS["min_days_7d"] and item["days_baseline"] >= READINESS["min_days_baseline"]


def readiness(summ: dict) -> dict:
    """Per metric of summary(): its trend and whether it's a readiness signal, by the planner's rule. Metrics without
    enough data for the planner (trusted) are left out, so the gauges and the check-in never see what it ignores."""
    out = {}
    if (h := summ.get("hrv")) and trusted(h) and "change_pct" in h:
        out["hrv"] = {"change_pct": h["change_pct"], "signal": h["change_pct"] <= READINESS["hrv_drop_pct"]}
    if (r := summ.get("rhr")) and trusted(r) and "change" in r:
        out["rhr"] = {"change": r["change"], "signal": r["change"] >= READINESS["rhr_rise_bpm"]}
    if (s := summ.get("sleep")) and trusted(s) and s["mean_7d"] is not None:
        out["sleep"] = {"mean_7d": s["mean_7d"], "baseline_mean": s["baseline_mean"],
                        "signal": s["mean_7d"] < READINESS["sleep_min_h"]}
    return out
