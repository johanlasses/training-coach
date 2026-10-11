"""Minimal Strava API client: token refresh, activity list, streams. Rate-limit aware."""
from __future__ import annotations

import logging
import time
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from .db import DB

log = logging.getLogger(__name__)

API = "https://www.strava.com/api/v3"
TOKEN_URL = "https://www.strava.com/oauth/token"

# Strava redirects here after approval. Nothing listens on it: the user copies the URL from the
# address bar back into the Settings page. Works without configuring a callback domain for HA.
REDIRECT_URI = "http://localhost/strava-callback"
SCOPES = "read,activity:read_all,profile:read_all"

RIDE_TYPES = {"Ride", "VirtualRide", "GravelRide", "MountainBikeRide"}


class RateLimited(Exception):
    pass


class StravaClient:
    """Tokens are rotated by Strava on refresh, so they live in the DB, never in settings."""

    def __init__(self, db: DB, client_id: str, client_secret: str, http: httpx.AsyncClient | None = None):
        self.db = db
        self.client_id = client_id
        self.client_secret = client_secret
        self.http = http or httpx.AsyncClient(timeout=30)
        # Short-term usage as reported by Strava (requests in current 15-min window, limit)
        self.usage_15min = 0
        self.limit_15min = 100

    @property
    def has_app(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @property
    def configured(self) -> bool:
        return self.has_app and bool((self.db.get("strava_token") or {}).get("refresh_token"))

    def authorize_url(self) -> str:
        return "https://www.strava.com/oauth/authorize?" + urlencode({
            "client_id": self.client_id, "response_type": "code", "redirect_uri": REDIRECT_URI,
            "approval_prompt": "force", "scope": SCOPES,
        })

    async def exchange_code(self, pasted: str) -> dict:
        """Accepts the full redirected URL (or just the code). Stores tokens, returns the athlete."""
        q = parse_qs(urlparse(pasted.strip()).query)
        if "error" in q:
            raise ValueError(f"Strava returned an error: {q['error'][0]}")
        code = q["code"][0] if "code" in q else pasted.strip()
        granted = set(q.get("scope", [SCOPES])[0].split(","))
        if "activity:read_all" not in granted:
            raise ValueError("Strava access to activities was not granted — tick all boxes on the Strava page")
        r = await self.http.post(TOKEN_URL, data={
            "client_id": self.client_id, "client_secret": self.client_secret,
            "code": code, "grant_type": "authorization_code",
        })
        if r.status_code >= 400:
            raise ValueError(f"Strava rejected the code ({r.status_code}). Codes are single-use — connect again.")
        data = r.json()
        self.db.set("strava_token", {k: data[k] for k in ("access_token", "refresh_token", "expires_at")})
        athlete = data.get("athlete") or {}
        self.db.set("strava_athlete", {k: athlete.get(k) for k in ("id", "firstname", "lastname", "profile_medium")})
        return athlete

    def disconnect(self) -> None:
        self.db.set("strava_token", None)
        self.db.set("strava_athlete", None)

    async def _token(self) -> str:
        tok = self.db.get("strava_token") or {}
        if not tok.get("refresh_token"):
            raise RuntimeError("Strava is not connected")
        if tok.get("access_token") and tok.get("expires_at", 0) > time.time() + 120:
            return tok["access_token"]
        r = await self.http.post(TOKEN_URL, data={
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "refresh_token",
            "refresh_token": tok["refresh_token"],
        })
        r.raise_for_status()
        data = r.json()
        self.db.set("strava_token", {k: data[k] for k in ("access_token", "refresh_token", "expires_at")})
        return data["access_token"]

    async def _get(self, path: str, **params) -> httpx.Response:
        if self.usage_15min >= self.limit_15min - 5:
            raise RateLimited(f"near 15-min limit ({self.usage_15min}/{self.limit_15min})")
        r = await self.http.get(f"{API}{path}", params=params,
                                headers={"Authorization": f"Bearer {await self._token()}"})
        self._track(r)
        if r.status_code == 429:
            raise RateLimited("429 from Strava")
        r.raise_for_status()
        return r

    def _track(self, r: httpx.Response) -> None:
        # Prefer the read-specific headers when present.
        usage = r.headers.get("x-readratelimit-usage") or r.headers.get("x-ratelimit-usage")
        limit = r.headers.get("x-readratelimit-limit") or r.headers.get("x-ratelimit-limit")
        if usage and limit:
            self.usage_15min = int(usage.split(",")[0])
            self.limit_15min = int(limit.split(",")[0])

    async def athlete(self) -> dict:
        return (await self._get("/athlete")).json()

    async def activities_since(self, after_epoch: int) -> list[dict]:
        out: list[dict] = []
        page = 1
        while True:
            batch = (await self._get("/athlete/activities", after=after_epoch, per_page=200, page=page)).json()
            out.extend(batch)
            if len(batch) < 200:
                return out
            page += 1

    async def streams(self, activity_id: int) -> dict[str, list]:
        r = await self._get(f"/activities/{activity_id}/streams",
                            keys="time,watts,heartrate", key_by_type="true")
        return {k: v["data"] for k, v in r.json().items()}


def summary_from_strava(a: dict) -> dict:
    return {
        "id": a["id"],
        "name": a.get("name"),
        "sport_type": a.get("sport_type") or a.get("type"),
        "start_date": a.get("start_date"),
        "start_date_local": (a.get("start_date_local") or "").rstrip("Z"),
        "distance": a.get("distance"),
        "moving_time": a.get("moving_time"),
        "elapsed_time": a.get("elapsed_time"),
        "elevation_gain": a.get("total_elevation_gain"),
        "trainer": int(bool(a.get("trainer"))),
        "avg_watts": a.get("average_watts"),
        "weighted_avg_watts": a.get("weighted_average_watts"),
        "max_watts": a.get("max_watts"),
        "device_watts": int(bool(a.get("device_watts"))),
        "avg_hr": a.get("average_heartrate"),
        "max_hr": a.get("max_heartrate"),
        "kilojoules": a.get("kilojoules"),
        "avg_speed": a.get("average_speed"),
    }
