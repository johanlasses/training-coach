"""Garmin Connect Personal API: the athlete's own rides and health data, pulled with their personal API key.

Garmin's Personal API (developer.garmin.com/gc-developer-program/personal) is for one user's own data and pull only,
which fits an add-on without a public URL. The athlete creates a key on Garmin Connect web (valid 30 days to a year,
shown once) and saves it on Profile. A key buys a 24-hour access token; there's no refresh token, so a new one is
minted from the key when it runs out. The endpoints are from Garmin's OpenAPI spec (dev/fakes/garmin_personal_openapi.json).
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import time
import zipfile
from datetime import date, datetime, timedelta, timezone

import fitdecode
import httpx

from .db import DB
from .strava import RateLimited

log = logging.getLogger(__name__)

# TC_GARMIN_URL points the app at a fake Garmin (dev/ stack); unset in production
BASE = os.environ.get("TC_GARMIN_URL", "https://personal.connectapi.garmin.com").rstrip("/")
TOKEN_PATH = "/personalapitoken-service/personal-api/personal-api-key/token"
TOKEN_HOURS = 24          # Garmin: "valid for 24 hours or until the key expires"
RENEW_BEFORE = 3600       # mint a new token this long before the old one runs out
PAGE = 100
HEALTH_DAYS_PER_CALL = 28  # sleep allows 31 days per call, steps 28: stay under both

# Garmin's cycling activity types → the Strava sport types the rest of the app uses. E-bikes keep Strava's e-bike types,
# which RIDE_TYPES leaves out like on Strava (motor-assisted load isn't the athlete's); hand cycling and BMX aren't mapped.
SPORT_TYPES = {"road_biking": "Ride", "cycling": "Ride", "gravel_cycling": "GravelRide", "mountain_biking": "MountainBikeRide",
               "cyclocross": "Ride", "track_cycling": "Ride", "indoor_cycling": "Ride", "virtual_ride": "VirtualRide",
               "recumbent_cycling": "Ride", "e_bike_fitness": "EBikeRide", "e_bike_mountain": "EMountainBikeRide"}
INDOOR = {"indoor_cycling", "virtual_ride"}


class GarminError(Exception):
    pass


class GarminClient:
    """Rides and health data from the Personal API. The key is a setting (Profile, never rendered back); the access
    token lives in the DB."""

    def __init__(self, db: DB, api_key: str, http: httpx.AsyncClient | None = None):
        self.db = db
        self.api_key = api_key.strip()
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10))

    @property
    def configured(self) -> bool:
        """A key that Garmin hasn't refused yet."""
        return bool(self.api_key) and not (self.db.get("garmin_status") or {}).get("refused")

    def status(self) -> dict:
        return self.db.get("garmin_status") or {}

    def disconnect(self) -> None:
        self.db.set("garmin_token", None)
        self.db.set("garmin_status", None)

    async def check(self) -> None:
        """Mints a fresh token with the key: raises GarminError when Garmin refuses it, RateLimited when it's busy."""
        self.db.set("garmin_token", None)
        await self._token()

    async def _token(self) -> str:
        tok = self.db.get("garmin_token") or {}
        if tok.get("access_token") and tok.get("expires_at", 0) > time.time() + RENEW_BEFORE:
            return tok["access_token"]
        r = await self.http.post(f"{BASE}{TOKEN_PATH}", headers={"Authorization": f"ApiKey {self.api_key}"})
        if r.status_code == 401:
            # Expired, deleted or mistyped (Garmin answers 401). Only this marks the key refused: a Cloudflare 403 or a
            # maintenance 404 is an outage, and Garmin shows a key once, so a refusal costs the athlete a new key.
            self.db.set("garmin_status", {"refused": True, "at": datetime.now().isoformat(timespec="seconds"),
                                          "error": "Garmin refused the API key"})
            raise GarminError("Garmin refused the API key. It may have expired: create a new one on Garmin Connect "
                              "and save it on Profile")
        if r.status_code == 429:
            raise RateLimited("Garmin rate limit reached, trying again on the next sync")
        if r.status_code >= 400:
            raise GarminError(f"Garmin's sign-in answered {r.status_code}; trying again on the next sync")
        access, expires_at = parse_token(r)
        self.db.set("garmin_token", {"access_token": access, "expires_at": expires_at})
        self.db.set("garmin_status", {"ok": True, "at": datetime.now().isoformat(timespec="seconds")})
        return access

    async def _get(self, path: str, **params) -> httpx.Response:
        r = await self.http.get(f"{BASE}{path}", params=params,
                                headers={"Authorization": f"Bearer {await self._token()}", "Accept": "application/json"})
        if r.status_code == 401:  # the token died early (key deleted): mint a new one once
            self.db.set("garmin_token", None)
            r = await self.http.get(f"{BASE}{path}", params=params,
                                    headers={"Authorization": f"Bearer {await self._token()}", "Accept": "application/json"})
        if r.status_code == 429:
            raise RateLimited("Garmin rate limit reached, trying again on the next sync")
        r.raise_for_status()
        return r

    # --- activities (the same interface sync.sync uses for Strava) ---------------------------------
    async def activities_since(self, after_epoch: int) -> list[dict]:
        # Garmin filters on the local start date; a day early so no evening ride west of UTC falls off the window
        start = (datetime.fromtimestamp(after_epoch, timezone.utc) - timedelta(days=1)).date().isoformat()
        out, offset = [], 0
        while True:
            # No activityType filter: the sports the app handles are whatever SPORT_TYPES maps (running joins it later)
            r = await self._get("/activitylist-service/activities/search/activities",
                                startDate=start, start=offset, limit=PAGE, sortOrder="asc")
            page = _json(r, [])
            if not page:  # until an empty page: the maximum page size isn't documented, a short page proves nothing
                return out
            out += page
            offset += len(page)

    @staticmethod
    def summary(a: dict) -> dict:
        return summary_from_garmin(a)

    async def streams(self, activity_id: int) -> dict[str, list]:
        """Power and HR from the original FIT file. A download that isn't a readable FIT (an HTML error page, a corrupt
        zip) gives no streams, like Strava's 404: otherwise that ride would stop every sync."""
        r = await self._get(f"/download-service/files/activity/{activity_id}")
        try:
            return await asyncio.to_thread(fit_streams, r.content)  # a long ride's file takes a moment to parse
        except (fitdecode.FitError, zipfile.BadZipFile, EOFError, ValueError) as e:
            log.warning("Garmin activity %s: the download isn't a readable FIT file (%s), skipping its streams", activity_id, e)
            return {}

    # --- health -------------------------------------------------------------------------------------
    async def health_days(self, first: date, last: date) -> dict[str, dict[str, float]]:
        """Date → {metric: value} (rhr bpm, hrv ms, sleep h, weight kg) from first to last, in chunks Garmin accepts.
        Each metric is read on its own: no data for one (no scale, say) mustn't lose the others."""
        out: dict[str, dict[str, float]] = {}
        lo = first
        while lo <= last:
            hi = min(lo + timedelta(days=HEALTH_DAYS_PER_CALL - 1), last)
            for read in (self._hrv, self._sleep, self._rhr, self._weight):
                try:
                    await read(lo, hi, out)
                except (httpx.HTTPError, ValueError, AttributeError, TypeError) as e:
                    log.warning("Garmin health %s %s..%s: %s", read.__name__.strip("_"), lo, hi, e)
            lo = hi + timedelta(days=1)
        return out

    async def _hrv(self, lo: date, hi: date, out: dict) -> None:
        r = await self._get(f"/hrv-service/hrv/daily/{lo}/{hi}")
        for s in _json(r, {}).get("hrvSummaries") or []:
            if s.get("lastNightAvg"):
                out.setdefault(s["calendarDate"], {})["hrv"] = float(s["lastNightAvg"])

    async def _sleep(self, lo: date, hi: date, out: dict) -> None:
        r = await self._get("/sleep-service/sleep/dailySleepSummariesByDate", startDate=lo.isoformat(), endDate=hi.isoformat())
        for s in _json(r, []):
            if s.get("sleepTimeSeconds"):
                out.setdefault(s["calendarDate"], {})["sleep"] = round(s["sleepTimeSeconds"] / 3600, 2)

    async def _rhr(self, lo: date, hi: date, out: dict) -> None:
        r = await self._get("/usersummary-service/v2/dailysummaries", startDate=lo.isoformat(), endDate=hi.isoformat())
        for s in _json(r, {}).get("data") or []:
            # Without wellness data restingValue is the day's minimum, not a resting HR: it would hide a rise
            if s.get("includesWellnessData", True) and (rhr := (s.get("heartRate") or {}).get("restingValue")):
                out.setdefault(s["calendarDate"], {})["rhr"] = float(rhr)
        # The summaries can come without heart rate (seen live): ask the per-day endpoint for the days still missing
        day = lo
        while day <= hi:
            if "rhr" not in out.get(day.isoformat(), {}):
                r = await self._get("/wellness-service/wellness/dailyHeartRate", date=day.isoformat())
                if (rhr := _json(r, {}).get("restingHeartRate")):
                    out.setdefault(day.isoformat(), {})["rhr"] = float(rhr)
            day += timedelta(days=1)

    async def _weight(self, lo: date, hi: date, out: dict) -> None:
        entries = sorted(_json(await self._get(f"/weight-service/weight/all/{lo}/{hi}"), []),
                         key=lambda s: s.get("timestampGMT") or 0, reverse=True)
        for s in entries:  # latest first, so the morning (earliest) weigh-in of a day wins
            if s.get("weight") and s.get("calendarDate"):
                kg = float(s["weight"])
                kg = kg / 1000 if kg > 300 else kg  # the spec says kg; Garmin's older weight APIs answered in grams
                out.setdefault(s["calendarDate"], {})["weight"] = round(kg, 2)


def _json(r: httpx.Response, default):
    """The body as JSON, or `default` for Garmin's "no data" (204 or an empty body) or a body of the wrong shape."""
    if r.status_code == 204 or not r.content:
        return default
    data = r.json()
    return data if isinstance(data, type(default)) else default


def jwt_exp(token: str) -> float | None:
    """The exp claim of a JWT, without verifying it (only used to know when to mint a new one)."""
    try:
        payload = token.split(".")[1]
        return float(json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["exp"])
    except (IndexError, ValueError, KeyError, TypeError):
        return None


def parse_token(r: httpx.Response) -> tuple[str, float]:
    """(access token, expiry epoch). Garmin answers with the bare JWT as plain text (checked live, Oct 2026); the
    usual JSON shapes (access_token / accessToken / token, expires_in or an expiry timestamp) are accepted too."""
    now = time.time()
    try:
        data = r.json()
    except ValueError:
        data = r.text.strip().strip('"')
    if isinstance(data, str):
        # A Cloudflare or maintenance page also comes back 200: only something token-shaped is a token
        if not data or len(data) > 4096 or any(c.isspace() or c in "<>" for c in data):
            raise GarminError("Garmin's sign-in answered with a page instead of a token; trying again on the next sync")
        return data, jwt_exp(data) or now + TOKEN_HOURS * 3600
    if not isinstance(data, dict):
        raise GarminError("Unexpected token response from Garmin; trying again on the next sync")
    token = next((data[k] for k in ("access_token", "accessToken", "token", "personalApiToken") if data.get(k)), None)
    if not token:
        raise GarminError(f"Unexpected token response from Garmin (fields: {', '.join(data)})")
    expires_in = data.get("expires_in") or data.get("expiresIn")
    expires_at = data.get("expires_at") or data.get("expiresAt") or data.get("expirationTimestamp")
    if expires_in:
        return token, now + float(expires_in)
    if expires_at:
        try:
            return token, float(expires_at) / (1000 if float(expires_at) > 1e12 else 1)
        except (TypeError, ValueError):
            try:
                return token, datetime.fromisoformat(str(expires_at).replace("Z", "+00:00")).timestamp()
            except ValueError:
                pass
    return token, now + TOKEN_HOURS * 3600


def _iso_z(gmt: str | None) -> str | None:
    """Garmin's "yyyy-MM-dd HH:mm:ss" GMT → Strava's "yyyy-MM-ddTHH:mm:ssZ"."""
    return gmt.replace(" ", "T")[:19] + "Z" if gmt else None


def summary_from_garmin(a: dict) -> dict:
    key = (a.get("activityType") or {}).get("typeKey", "")
    timer = a.get("duration")  # the device's timer time, which avgPower is averaged over
    # Moving time comes from speed: indoors without a speed sensor it runs short, so the timer counts there
    secs = (timer if key in INDOOR else a.get("movingDuration")) or timer
    return {
        "id": a["activityId"],
        "name": a.get("activityName"),
        "sport_type": SPORT_TYPES.get(key, "Other"),
        "start_date": _iso_z(a.get("startTimeGMT")),
        "start_date_local": (a.get("startTimeLocal") or "").replace(" ", "T")[:19],
        "distance": a.get("distance"),
        "moving_time": round(secs) if secs else None,
        "elapsed_time": round(a["elapsedDuration"]) if a.get("elapsedDuration") else None,
        "elevation_gain": a.get("elevationGain"),
        "trainer": int(key in INDOOR),
        "avg_watts": a.get("avgPower"),
        "weighted_avg_watts": a.get("normPower"),
        "max_watts": a.get("maxPower"),
        "device_watts": int(bool(a.get("avgPower"))),
        "avg_hr": a.get("averageHR"),
        "max_hr": a.get("maxHR"),
        "kilojoules": round(a["avgPower"] * (timer or secs) / 1000, 1) if a.get("avgPower") and (timer or secs) else None,
        "avg_speed": a.get("averageSpeed"),
    }


def fit_streams(content: bytes) -> dict[str, list]:
    """Garmin's activity download (a zip holding the original .fit, or the .fit itself) → Strava-style streams:
    seconds from the start, watts and heart rate per record."""
    if content[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            name = next((n for n in z.namelist() if n.lower().endswith(".fit")), None)
            if not name:
                return {}
            content = z.read(name)
    times, watts, hr = [], [], []
    start = None
    with fitdecode.FitReader(io.BytesIO(content), check_crc=fitdecode.CrcCheck.WARN) as fit:
        for frame in fit:
            if frame.frame_type != fitdecode.FIT_FRAME_DATA or frame.name != "record" or not frame.has_field("timestamp"):
                continue
            ts = frame.get_value("timestamp")
            if not isinstance(ts, datetime):
                continue
            start = start or ts
            t = int((ts - start).total_seconds())
            if times and t <= times[-1]:
                continue  # a repeated timestamp would add a sample
            times.append(t)
            watts.append(_num(frame, "power"))
            hr.append(_num(frame, "heart_rate"))
    out: dict[str, list] = {"time": times} if times else {}
    if any(watts):
        out["watts"] = watts
    if any(hr):
        out["heartrate"] = hr
    return out


def _num(frame, field: str) -> float:
    v = frame.get_value(field, fallback=None) if frame.has_field(field) else None
    return float(v) if isinstance(v, (int, float)) else 0.0
