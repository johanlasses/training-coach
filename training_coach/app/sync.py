"""Activity source (Strava or Garmin) → DB sync, stream processing and FTP/TSS recompute."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Protocol

import httpx

from . import metrics, sports
from .config import Config
from .db import DB
from .strava import RateLimited

log = logging.getLogger(__name__)

BACKFILL_DAYS = 365
RESYNC_DAYS = 14          # re-list recent window to catch edits/deletions
STREAMS_PER_RUN = 40
DEFAULT_FTP = 200
EFTP_WINDOW_DAYS = 90
TEST_VALID_DAYS = 84  # a field test sets FTP for 12 weeks, then eFTP takes over again


class Source(Protocol):
    """What sync needs from StravaClient and GarminClient. A source raises RateLimited near its limits."""
    async def activities_since(self, after_epoch: int) -> list[dict]: ...
    def summary(self, a: dict) -> dict: ...  # the source's activity → db.ACTIVITY_COLUMNS, Strava's sport types
    async def streams(self, activity_id: int) -> dict[str, list]: ...


async def sync(db: DB, strava: Source, cfg: Config) -> dict:
    """One sync pass. Safe to call repeatedly; backfills streams gradually within rate limits."""
    now = datetime.now(timezone.utc)
    last = db.get("last_activity_start")
    # Runs and walks sync since 0.11.0: a DB from before lists the whole year once, or the run history (and the run
    # durability guard built on it) would start two weeks ago
    if last and db.get("kinds_backfilled"):
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
    rides = [s for s in map(strava.summary, listed) if s["sport_type"] in sports.KEPT]  # rides, runs and walks
    new = sum(db.upsert_activity_summary(a) for a in rides)
    db.set("kinds_backfilled", True)
    if db.get("mark_notified"):  # the first sync after a source switch: these rides had their push from the old source
        db.mark_all_notified()
        db.set("mark_notified", None)
    if last:
        db.delete_activities_not_in({a["id"] for a in rides}, after.strftime("%Y-%m-%dT%H:%M:%SZ"))
    if rides:
        newest = max(a["start_date"] for a in rides)
        if not last or newest > last:
            db.set("last_activity_start", newest)

    fetched = 0
    try:
        for a in db.activities_missing_streams(STREAMS_PER_RUN):
            try:
                s = await strava.streams(a["id"])
            except httpx.HTTPStatusError as e:
                if e.response.status_code != 404:
                    raise
                # Manual or private activities have no streams. Mark them done, or every sync dies on them.
                log.warning("No streams for activity %s (404), skipping", a["id"])
                s = {}
            db.update_activity(a["id"], streams_fetched=1, **process_streams(s, ride=sports.is_ride(a)))
            fetched += 1
    except RateLimited as e:
        log.info("Stopping stream fetch: %s", e)
    except Exception:
        recompute(db, cfg)  # score what was stored, so a failing ride can't leave others without FTP
        raise

    recompute(db, cfg)
    result = {"at": now.isoformat(timespec="seconds"), "listed": len(rides), "new": new,
              "streams": fetched, "pending_streams": len(db.activities_missing_streams(10_000)), "rate_limited": ""}
    db.set("last_sync", result)
    return result


def process_streams(s: dict[str, list], ride: bool = True) -> dict:
    """Power numbers only for rides: running power (a Stryd) isn't cycling power, and must never reach the power
    curve, zones or eFTP."""
    times = s.get("time") or []
    out: dict = {}
    watts = metrics.resample_1hz(times, s["watts"]) if s.get("watts") and ride else []
    hr = metrics.resample_1hz(times, s["heartrate"]) if s.get("heartrate") else []
    if watts and max(watts) > 0:
        out["np"] = metrics.normalized_power(watts)
        out["mmp"] = metrics.mmp(watts)
        out["power_hist"] = metrics.power_histogram(watts)
        out["spark_watts"] = metrics.downsample(watts, 240)  # ~15 s blocks on an hour: intervals stay visible
        if hr:
            out["decoupling"] = metrics.decoupling(watts, hr)
    if hr:
        out["spark_hr"] = metrics.downsample(hr)
    return out


def ftp_at(acts_by_date: list[dict], day: date, override: int, tests: list[dict] = ()) -> tuple[float, str]:
    """FTP in effect on `day`: override, else the latest field test in the 12 weeks up to `day`, else eFTP from
    best efforts in the prior 90 days. A test only counts from its own date, so it never rewrites older rides."""
    if override:
        return float(override), "override"
    lo_test = (day - timedelta(days=TEST_VALID_DAYS)).isoformat()
    valid = [t for t in tests if lo_test <= t["date"] <= day.isoformat()]
    if valid:
        return float(max(valid, key=lambda t: t["date"])["ftp"]), "test"
    lo = (day - timedelta(days=EFTP_WINDOW_DAYS)).isoformat()
    hi = (day + timedelta(days=1)).isoformat()
    curves = [a["mmp"] for a in acts_by_date if a.get("mmp") and sports.is_ride(a) and lo <= a["start_date_local"] < hi]
    e = metrics.eftp_from_mmp(metrics.best_curve(curves))
    return (float(e), "eftp") if e else (float(DEFAULT_FTP), "default")


def threshold_pace_at(acts: list[dict], day: date, cfg: Config) -> float | None:
    """Threshold pace (s/km) on `day`: the Profile's, else estimated from the 90 days of runs before it."""
    if cfg.threshold_pace:
        return float(cfg.threshold_pace)
    lo, hi = (day - timedelta(days=EFTP_WINDOW_DAYS)).isoformat(), (day + timedelta(days=1)).isoformat()
    # Runs with long stops (standing recoveries between reps) average out at rep pace: leave them out
    return metrics.threshold_pace([(a["moving_time"] or 0, a["avg_speed"] or 0) for a in sports.runs(acts)
                                   if lo <= a["start_date_local"] < hi
                                   and (a["elapsed_time"] or 0) <= 1.15 * (a["moving_time"] or 0)])


def run_load(a: dict, acts: list[dict], cfg: Config) -> tuple[float, float | None, str]:
    """(TSS, IF, source) for a run or walk. A run: hrTSS against the running threshold HR first (pace misreads hills,
    trails and treadmills), else rTSS from pace, else a flat estimate. A walk: always the flat 20 TSS/h (IF ~0.45),
    because (HR/LTHR)² ignores resting HR and would score a walk about twice that just for wearing a watch."""
    secs = a["moving_time"] or 0
    if sports.kind(a["sport_type"]) == "walk":
        return metrics.estimate_tss(secs, 20), None, "estimate"
    if a.get("avg_hr"):
        tss, intensity = metrics.tss_from_hr(secs, a["avg_hr"], cfg.run_threshold_hr)
        return tss, intensity, "rhr"
    pace = threshold_pace_at(acts, date.fromisoformat(a["start_date_local"][:10]), cfg)
    if pace and a.get("avg_speed"):
        tss, intensity = metrics.tss_from_pace(secs, a["avg_speed"], pace)
        return tss, intensity, "rpace"
    return metrics.estimate_tss(secs, 55), None, "estimate"  # an easy run, ~IF 0.75


def recompute(db: DB, cfg: Config) -> None:
    """Assign FTP/TSS/IF to every activity. Cheap enough to run after each sync."""
    acts = db.activities()
    for a in acts:
        if not sports.is_ride(a):
            tss, intensity, src = run_load(a, acts, cfg)
            db.update_activity(a["id"], ftp_used=None, tss=round(tss, 1),
                               intensity=round(intensity, 3) if intensity else None, tss_source=src)
            continue
        day = date.fromisoformat(a["start_date_local"][:10])
        ftp, _ = ftp_at(acts, day, cfg.ftp_override, cfg.ftp_tests)
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
