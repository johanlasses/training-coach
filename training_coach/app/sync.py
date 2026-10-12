"""Strava → DB sync, stream processing and FTP/TSS recompute."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from . import metrics
from .config import Config
from .db import DB
from .strava import RIDE_TYPES, RateLimited, StravaClient, summary_from_strava

log = logging.getLogger(__name__)

BACKFILL_DAYS = 365
RESYNC_DAYS = 14          # re-list recent window to catch edits/deletions
STREAMS_PER_RUN = 40
DEFAULT_FTP = 200
EFTP_WINDOW_DAYS = 90


async def sync(db: DB, strava: StravaClient, cfg: Config) -> dict:
    """One sync pass. Safe to call repeatedly; backfills streams gradually within rate limits."""
    now = datetime.now(timezone.utc)
    last = db.get("last_activity_start")
    if last:
        after = datetime.fromisoformat(last.replace("Z", "+00:00")) - timedelta(days=RESYNC_DAYS)
    else:
        after = now - timedelta(days=BACKFILL_DAYS)

    try:
        listed = await strava.activities_since(int(after.timestamp()))
    except RateLimited as e:
        # Not a failure: the next scheduled sync picks up where this one would have.
        log.info("Sync skipped: %s", e)
        result = (db.get("last_sync") or {}) | {"at": now.isoformat(timespec="seconds"), "rate_limited": str(e)}
        db.set("last_sync", result)
        return result
    rides = [a for a in listed if (a.get("sport_type") or a.get("type")) in RIDE_TYPES]
    new = sum(db.upsert_activity_summary(summary_from_strava(a)) for a in rides)
    if last:
        db.delete_activities_not_in({a["id"] for a in rides}, after.strftime("%Y-%m-%dT%H:%M:%SZ"))
    if rides:
        newest = max(a["start_date"] for a in rides)
        if not last or newest > last:
            db.set("last_activity_start", newest)

    fetched = 0
    try:
        for a in db.activities_missing_streams(STREAMS_PER_RUN):
            s = await strava.streams(a["id"])
            db.update_activity(a["id"], streams_fetched=1, **process_streams(s))
            fetched += 1
    except RateLimited as e:
        log.info("Stopping stream fetch: %s", e)

    recompute(db, cfg)
    result = {"at": now.isoformat(timespec="seconds"), "listed": len(rides), "new": new,
              "streams": fetched, "pending_streams": len(db.activities_missing_streams(10_000)), "rate_limited": ""}
    db.set("last_sync", result)
    return result


def process_streams(s: dict[str, list]) -> dict:
    times = s.get("time") or []
    out: dict = {}
    watts = metrics.resample_1hz(times, s["watts"]) if s.get("watts") else []
    hr = metrics.resample_1hz(times, s["heartrate"]) if s.get("heartrate") else []
    if watts and max(watts) > 0:
        out["np"] = metrics.normalized_power(watts)
        out["mmp"] = metrics.mmp(watts)
        out["power_hist"] = metrics.power_histogram(watts)
        out["spark_watts"] = metrics.downsample(watts)
        if hr:
            out["decoupling"] = metrics.decoupling(watts, hr)
    if hr:
        out["spark_hr"] = metrics.downsample(hr)
    return out


def ftp_at(acts_by_date: list[dict], day: date, override: int) -> tuple[float, str]:
    """FTP in effect on `day`: override, else eFTP from best efforts in the prior 90 days."""
    if override:
        return float(override), "override"
    lo = (day - timedelta(days=EFTP_WINDOW_DAYS)).isoformat()
    hi = (day + timedelta(days=1)).isoformat()
    curves = [a["mmp"] for a in acts_by_date if a.get("mmp") and lo <= a["start_date_local"] < hi]
    e = metrics.eftp_from_mmp(metrics.best_curve(curves))
    return (float(e), "eftp") if e else (float(DEFAULT_FTP), "default")


def recompute(db: DB, cfg: Config) -> None:
    """Assign FTP/TSS/IF to every activity. Cheap enough to run after each sync."""
    acts = db.activities()
    for a in acts:
        day = date.fromisoformat(a["start_date_local"][:10])
        ftp, _ = ftp_at(acts, day, cfg.ftp_override)
        secs = a["moving_time"] or 0
        np = a.get("np") or (a.get("weighted_avg_watts") if a.get("device_watts") else None)
        if np:
            tss, intensity = metrics.tss_from_power(secs, np, ftp)
            src = "power"
        elif a.get("avg_hr"):
            tss, intensity = metrics.tss_from_hr(secs, a["avg_hr"], cfg.lthr)
            src = "hr"
        else:
            tss, intensity, src = metrics.estimate_tss(secs), None, "estimate"
        db.update_activity(a["id"], ftp_used=ftp, tss=round(tss, 1),
                           intensity=round(intensity, 3) if intensity else None, tss_source=src)
