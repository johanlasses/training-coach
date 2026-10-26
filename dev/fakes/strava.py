"""Fake Strava: OAuth, athlete, activities and power/HR streams, plus /_dev controls. Rides are deterministic per id."""
from __future__ import annotations

import random
import time
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse

app = FastAPI()
HISTORY_DAYS = 60
state = {"calls": 0, "fail_streams": set(), "rate_limit_next": 0, "extra": [], "athlete": 1}

KINDS = [  # name, sport, minutes, base watts, blocks (start_frac, len_s, watts)
    ("Zwift - Endurance in Watopia", "VirtualRide", 60, 165, []),
    ("Zwift - Sweet spot 3x12", "VirtualRide", 70, 160, [(0.2, 720, 245), (0.45, 720, 245), (0.7, 720, 245)]),
    ("Zwift - VO2 5x4", "VirtualRide", 60, 150, [(0.25 + i * 0.1, 240, 310) for i in range(5)]),
    ("Morning gravel loop", "GravelRide", 90, 170, [(0.5, 300, 300)]),
]


def make_ride(aid: int, start: datetime, kind: int) -> dict:
    name, sport, minutes, base, blocks = KINDS[kind]
    rnd = random.Random(aid)
    n = minutes * 60
    watts = [max(0, rnd.gauss(base, 18)) for _ in range(n)]
    for frac, length, w in blocks:
        for i in range(int(frac * n), min(n, int(frac * n) + length)):
            watts[i] = max(0, rnd.gauss(w, 8))
    hr = [110 + w * 0.17 + i / n * 6 for i, w in enumerate(watts)]
    return {"id": aid, "name": name, "sport_type": sport, "type": sport, "start": start, "watts": watts, "hr": hr,
            "n": n}


def make_run(aid: int, start: datetime, sport: str = "Run", minutes: int = 35) -> dict:
    """A run (or walk): HR only, like a watch without a foot pod. Runs sync since 0.11.0."""
    n = minutes * 60
    base = 100 if sport == "Walk" else 145
    return {"id": aid, "name": {"Run": "Morning run", "TrailRun": "Trail run", "Walk": "Lunch walk"}[sport],
            "sport_type": sport, "type": sport, "start": start, "watts": [0.0] * n, "hr": [base + i / n * 8 for i in range(n)],
            "n": n, "speed": 1.5 if sport == "Walk" else 2.9}


def history() -> list[dict]:
    rnd = random.Random(7)
    rides, today = [], datetime.now(timezone.utc).replace(hour=16, minute=0, second=0, microsecond=0)
    aid = 5000
    for back in range(HISTORY_DAYS, 0, -1):
        d = today - timedelta(days=back)
        if back % 5 == 0:  # a run every fifth morning, a walk now and then: some days hold a ride too
            aid += 1
            rides.append(make_run(aid, d.replace(hour=6), "Walk" if back % 15 == 0 else "TrailRun" if back % 10 == 0 else "Run"))
        if rnd.random() < 0.45:
            continue
        aid += 1
        rides.append(make_ride(aid, d, rnd.randrange(len(KINDS))))
    return rides


def all_rides() -> list[dict]:
    return history() + state["extra"]


def summary(r: dict) -> dict:
    w, n = r["watts"], r["n"]
    start = r["start"]
    speed, power = r.get("speed", 8.3), any(w)
    return {"id": r["id"], "name": r["name"], "sport_type": r["sport_type"], "type": r["type"],
            "start_date": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "start_date_local": start.strftime("%Y-%m-%dT%H:%M:%SZ"),  # Strava's "local" has a fake Z too
            "distance": n * speed, "moving_time": n, "elapsed_time": n + 60, "total_elevation_gain": 200,
            "trainer": r["sport_type"] == "VirtualRide", "average_watts": sum(w) / n if power else None,
            "weighted_average_watts": None, "max_watts": max(w) if power else None, "device_watts": power,
            "average_heartrate": sum(r["hr"]) / n, "max_heartrate": max(r["hr"]), "kilojoules": sum(w) / 1000 if power else None,
            "average_speed": speed}


def headers() -> dict:
    state["calls"] += 1
    return {"x-ratelimit-limit": "100,1000", "x-ratelimit-usage": f"{state['calls'] % 100},{state['calls']}",
            "x-readratelimit-limit": "100,1000", "x-readratelimit-usage": f"{state['calls'] % 100},{state['calls']}"}


def limited():
    if state["rate_limit_next"] > 0:
        state["rate_limit_next"] -= 1
        return JSONResponse({"message": "Rate Limit Exceeded"}, status_code=429, headers=headers())


@app.get("/oauth/authorize")
def authorize(redirect_uri: str, scope: str = "read"):
    return RedirectResponse(f"{redirect_uri}?state=&code=dev-code&scope={scope}")


@app.post("/oauth/token")
async def token(request: Request):
    form = await request.form()
    if form.get("grant_type") == "authorization_code" and form.get("code") != "dev-code":
        return JSONResponse({"message": "Bad Request"}, status_code=400)
    a = state["athlete"]  # who is "signed in to Strava" for the next authorisation (/_dev/athlete)
    return {"token_type": "Bearer", "access_token": f"dev-access-{int(time.time())}", "refresh_token": "dev-refresh",
            "expires_at": int(time.time()) + 6 * 3600,
            "athlete": {"id": a, "firstname": "Dev", "lastname": "Rider" if a == 1 else f"Rider {a}", "profile_medium": ""}}


@app.post("/_dev/athlete")
def signed_in_as(id: int = 1):
    """The Strava account the next sign-in authorises: 1 is the owner's, 2 another athlete's."""
    state["athlete"] = id
    return {"athlete": id}


@app.get("/api/v3/athlete")
def athlete():
    return JSONResponse({"id": 1, "firstname": "Dev", "lastname": "Rider"}, headers=headers())


@app.get("/api/v3/athlete/activities")
def activities(after: int = 0, page: int = 1, per_page: int = 200):
    if (r := limited()):
        return r
    rows = [summary(r) for r in all_rides() if r["start"].timestamp() > after]
    return JSONResponse(rows[(page - 1) * per_page:page * per_page], headers=headers())


@app.get("/api/v3/activities/{aid}/streams")
def streams(aid: int):
    if (r := limited()):
        return r
    if aid in state["fail_streams"]:
        return JSONResponse({"message": "Record Not Found"}, status_code=404, headers=headers())
    ride = next((r for r in all_rides() if r["id"] == aid), None)
    if not ride:
        return JSONResponse({"message": "Record Not Found"}, status_code=404, headers=headers())
    n = ride["n"]
    out = {"time": {"data": list(range(n))}, "heartrate": {"data": [round(h) for h in ride["hr"]]}}
    if any(ride["watts"]):
        out["watts"] = {"data": [round(w) for w in ride["watts"]]}
    return JSONResponse(out, headers=headers())


@app.post("/_dev/ride")
def add_ride(kind: int = 0, minutes_ago: int = 30, sport: str = ""):
    """A ride that finished `minutes_ago` minutes ago (as the athlete would see after a Zwift session); with
    sport=Run|TrailRun|Walk a run or walk instead."""
    aid = 9000 + len(state["extra"])
    if sport:
        ride = make_run(aid, datetime.now(timezone.utc) - timedelta(minutes=minutes_ago + 35), sport)
    else:
        ride = make_ride(aid, datetime.now(timezone.utc) - timedelta(minutes=minutes_ago + KINDS[kind][2]), kind)
    state["extra"].append(ride)
    return {"id": aid, "name": ride["name"]}


@app.post("/_dev/fail")
def fail(stream_404: int | None = None, rate_limit: int | None = None):
    if stream_404:
        state["fail_streams"].add(stream_404)
    if rate_limit is not None:
        state["rate_limit_next"] = rate_limit
    return {"fail_streams": sorted(state["fail_streams"]), "rate_limit_next": state["rate_limit_next"]}


@app.post("/_dev/reset")
def reset():
    state.update(calls=0, fail_streams=set(), rate_limit_next=0, extra=[], athlete=1)
    return {"ok": True}
