"""Fake Garmin Connect Personal API: the token from an API key, the activity search, FIT downloads and the health
endpoints, shaped like garmin_personal_openapi.json. Key "dev-garmin-key" works; any other is refused."""
from __future__ import annotations

import random
import sys
from datetime import date, datetime, timedelta, timezone

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

sys.path.insert(0, "/opt/tests")  # the tests' FIT writer, so the fake and the parser's tests can't drift apart
from fitfile import fit_records, zipped  # noqa: E402

app = FastAPI()
KEY, TOKEN = "dev-garmin-key", "dev-garmin-token"
state = {"tokens": 0}


def activities() -> list[dict]:
    """Rides every other day over the last 6 weeks (Zwift and road), plus walks the sync must leave out."""
    out, today = [], date.today()
    for n in range(42, 0, -2):
        day = today - timedelta(days=n)
        key = "virtual_ride" if n % 4 else "road_biking"
        minutes = 60 if key == "virtual_ride" else 120
        start = datetime(day.year, day.month, day.day, 17, 30)
        out.append({"activityId": 9_000_000 + n, "activityName": "Zwift - Watopia" if key == "virtual_ride" else "Morning Ride",
                    "activityType": {"typeKey": key, "typeId": 1, "parentTypeId": 2},
                    "startTimeLocal": start.strftime("%Y-%m-%d %H:%M:%S"),
                    "startTimeGMT": (start - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"),
                    "distance": minutes * 500.0, "duration": minutes * 60.0, "movingDuration": minutes * 60.0,
                    "elapsedDuration": minutes * 60.0 + 60, "elevationGain": 300.0, "avgPower": 190.0, "normPower": 205.0,
                    "maxPower": 650.0, "averageHR": 138.0, "maxHR": 168.0, "averageSpeed": 8.3})
        if n % 6 == 0:
            out.append({"activityId": 8_000_000 + n, "activityName": "Walk", "activityType": {"typeKey": "walking"},
                        "startTimeLocal": f"{day} 12:00:00", "startTimeGMT": f"{day} 10:00:00", "duration": 3600.0,
                        "movingDuration": 3500.0, "distance": 5000.0})
    return out


def auth(request: Request) -> Response | None:
    if request.headers.get("Authorization") != f"Bearer {TOKEN}":
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return None


@app.post("/personalapitoken-service/personal-api/personal-api-key/token")
async def token(request: Request):
    if request.headers.get("Authorization") != f"ApiKey {KEY}":
        return JSONResponse({"error": "invalid api key"}, status_code=401)
    state["tokens"] += 1
    return Response(TOKEN, media_type="text/plain")  # live Garmin answers with the bare token, not JSON


@app.get("/activitylist-service/activities/search/activities")
def search(request: Request, start: int = 0, limit: int = 99, startDate: str = ""):
    if bad := auth(request):
        return bad
    acts = [a for a in activities() if not startDate or a["startTimeLocal"][:10] >= startDate]
    return acts[start:start + limit]


@app.get("/download-service/files/activity/{activity_id}")
def download(request: Request, activity_id: int):
    if bad := auth(request):
        return bad
    a = next((a for a in activities() if a["activityId"] == activity_id), None)
    if not a:
        return Response(status_code=404)
    secs = int(a["duration"])
    rng = random.Random(activity_id)
    watts = [max(0, int(190 + 40 * ((i // 300) % 2) + rng.randint(-15, 15))) for i in range(secs)]
    hr = [int(120 + 25 * min(1, i / 900) + rng.randint(-2, 2)) for i in range(secs)]
    start = datetime.fromisoformat(a["startTimeGMT"]).replace(tzinfo=timezone.utc)
    return Response(zipped(fit_records(start, watts, hr), f"{activity_id}_ACTIVITY.fit"), media_type="application/x-zip-compressed")


def days(a: str, b: str) -> list[date]:
    d, out = date.fromisoformat(a), []
    while d <= date.fromisoformat(b):
        out.append(d)
        d += timedelta(days=1)
    return out


@app.get("/hrv-service/hrv/daily/{a}/{b}")
def hrv(request: Request, a: str, b: str):
    if bad := auth(request):
        return bad
    return {"hrvSummaries": [{"calendarDate": d.isoformat(), "lastNightAvg": 52 + d.toordinal() % 7, "status": "BALANCED"}
                             for d in days(a, b)]}


@app.get("/sleep-service/sleep/dailySleepSummariesByDate")
def sleep(request: Request, startDate: str, endDate: str):
    if bad := auth(request):
        return bad
    return [{"calendarDate": d.isoformat(), "sleepTimeSeconds": int((7 + (d.toordinal() % 5) / 5) * 3600)} for d in days(startDate, endDate)]


@app.get("/usersummary-service/v2/dailysummaries")
def dailies(request: Request, startDate: str, endDate: str):
    if bad := auth(request):
        return bad
    # Live, the summaries came without heartRate: leave it out on odd days so the per-day fallback is exercised
    return {"data": [{"calendarDate": d.isoformat()} | ({"heartRate": {"restingValue": 47 + d.toordinal() % 3}} if d.toordinal() % 2 else {})
                     for d in days(startDate, endDate)]}


@app.get("/wellness-service/wellness/dailyHeartRate")
def daily_heart_rate(request: Request, date: str):
    if bad := auth(request):
        return bad
    return {"calendarDate": date, "restingHeartRate": 48, "lastSevenDaysAvgRestingHeartRate": 48}


@app.get("/weight-service/weight/all/{a}/{b}")
def weight(request: Request, a: str, b: str):
    if bad := auth(request):
        return bad
    return [{"calendarDate": b, "weight": 73.4}]


@app.get("/_dev/state")
def dev_state():
    return state


@app.post("/_dev/reset")
def reset():
    state["tokens"] = 0
    return state
