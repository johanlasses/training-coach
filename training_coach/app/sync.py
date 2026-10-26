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
    mark_duplicates(db)  # after deletions, before the stream fetch: a second recording costs no stream call
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
        # NP, zones and Pw:HR over the moving time (auto-paused stops left out, as TSS uses moving time); the power
        # curve and the sparkline on the elapsed timeline (a best never spans a stop, the plan overlay lines up)
        flags = s.get("moving")
        if flags and sum(map(bool, flags)) < len(flags) / 2:
            flags = None  # mostly "stopped": an indoor ride without a speed sensor, not a ride spent standing still
        moving = metrics.resample_1hz(times, s["watts"], moving=True, flags=flags)
        out["np"] = metrics.normalized_power(moving)
        out["mmp"] = metrics.mmp(watts)
        out["power_hist"] = metrics.power_histogram(moving)
        out["spark_watts"] = metrics.downsample(watts, 240)  # ~15 s blocks on an hour: intervals stay visible
        # Pw:HR assumes one continuous effort: after a stop HR restarts low while power jumps back, so a ride stopped
        # for more than a tenth of it reads better than it is
        if hr and len(moving) >= 0.9 * len(watts):
            out["decoupling"] = metrics.decoupling(moving, metrics.resample_1hz(times, s["heartrate"], moving=True, flags=flags))
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


OVERLAP = 0.5            # two recordings overlapping this much of the shorter one are one session...
START_WINDOW = 15 * 60   # ...if they also started within this of each other (a real session starts once)
FULL_SHARE = 0.9         # a recording holding this much of the longest one has the whole session


def _span(a: dict) -> tuple[float, float]:
    """(start, end) of a recording: elapsed time, but never more than 1.5 × moving time, so a ride saved hours later
    (the stop button forgotten) can't cover the evening's session."""
    start = datetime.fromisoformat(a["start_date"].replace("Z", "+00:00")).timestamp()
    moving = a["moving_time"] or 0
    return start, start + min(a["elapsed_time"] or moving, 1.5 * moving or (a["elapsed_time"] or 0))


def _keep(group: list[dict]) -> dict:
    """Which recording of a session to keep. Only one that holds nearly the whole session (FULL_SHARE of the longest):
    a Zwift crash mustn't cost the rest of the ride's load. Among those: Zwift's (its name matches the workout, its
    power is the trainer's), then one with power, then with HR, then the longer, then the first synced."""
    longest = max(a["moving_time"] or 0 for a in group)
    return min(group, key=lambda a: ((a["moving_time"] or 0) < FULL_SHARE * longest, a["sport_type"] != "VirtualRide",
                                      not a.get("device_watts"), not a.get("avg_hr"), -(a["moving_time"] or 0), a["id"]))


def find_duplicates(acts: list[dict]) -> dict[int, int]:
    """Second recordings of one session → the one kept. One athlete can't do two rides (or runs, or walks) at once, so
    two of the same kind that started within START_WINDOW and overlap by at least OVERLAP of the shorter one are the
    same session recorded twice: Zwift and the head unit both uploading an indoor ride, or two devices on one outdoor
    ride. Linear: sorted by start, a recording is only compared with those that started just before it."""
    spans = sorted(((*_span(a), a) for a in acts if a["start_date"] and sports.kind(a["sport_type"])), key=lambda x: x[0])
    group_of: dict[int, list[dict]] = {}
    recent: list[tuple[float, float, dict]] = []
    for s, e, a in spans:
        recent = [r for r in recent if s - r[0] <= START_WINDOW]
        match = next((b for bs, be, b in recent if sports.kind(b["sport_type"]) == sports.kind(a["sport_type"])
                      and min(e, be) - max(s, bs) >= OVERLAP * min(e - s, be - bs) > 0), None)
        group = group_of[match["id"]] if match else []
        group.append(a)
        group_of[a["id"]] = group
        recent.append((s, e, a))
    out = {}
    for group in {id(g): g for g in group_of.values()}.values():
        if len(group) > 1:
            keep = _keep(group)
            out |= {a["id"]: keep["id"] for a in group if a is not keep}
    return out


def mark_duplicates(db: DB) -> dict[int, int]:
    """Finds second recordings and takes them out of load, lists and views (db.activities). A session is pushed once:
    if any recording of it had its push, all of them count as notified, and feedback goes with it (so deleting the
    kept one on Strava never brings a second push). A Zwift file without HR gets the watch's average HR."""
    acts = db.activities(duplicates=True)
    dupes = find_duplicates(acts)
    by_id = {a["id"]: a for a in acts}
    groups: dict[int, list[dict]] = {}
    for d, k in dupes.items():
        groups.setdefault(k, [by_id[k]]).append(by_id[d])
        if by_id[d]["duplicate_of"] != k:
            log.info("Activity %s (%s) is a second recording of %s (%s)", d, by_id[d]["name"], k, by_id[k]["name"])
    for k, group in groups.items():
        keep = by_id[k]
        fb = next((a["feedback"] for a in group if a.get("feedback")), None)
        if any(a["notified"] for a in group):
            for a in group:
                if not a["notified"] or (fb and not a.get("feedback")):
                    db.update_activity(a["id"], notified=1, **({"feedback": fb} if fb and not a.get("feedback") else {}))
        if not keep.get("avg_hr") and (hr := next((a for a in group if a.get("avg_hr")), None)):
            db.update_activity(k, avg_hr=hr["avg_hr"], max_hr=hr.get("max_hr"))
    db.set_duplicates(dupes)
    return dupes


def recompute(db: DB, cfg: Config) -> None:
    """Assign FTP/TSS/IF to every activity. Cheap enough to run after each sync."""
    mark_duplicates(db)
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
