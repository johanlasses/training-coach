import time

import httpx
import pytest
import respx

from app.config import Config
from app.db import DB
from app.strava import API, TOKEN_URL, StravaClient
from app.sync import sync


def strava_activity(i, sport="VirtualRide", start="2026-10-01T16:00:00Z", watts=True):
    return {"id": i, "name": f"Ride {i}", "sport_type": sport, "start_date": start,
            "start_date_local": start, "distance": 30000, "moving_time": 3600, "elapsed_time": 3700,
            "total_elevation_gain": 300, "trainer": sport == "VirtualRide", "average_watts": 200 if watts else None,
            "weighted_average_watts": 210 if watts else None, "device_watts": watts,
            "average_heartrate": 140, "max_heartrate": 170, "kilojoules": 720}


@pytest.fixture
def db(tmp_path):
    return DB(tmp_path / "t.db")


@respx.mock
async def test_sync_imports_rides_and_streams(db):
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={
        "access_token": "a1", "refresh_token": "r2", "expires_at": int(time.time()) + 3600}))
    respx.get(f"{API}/athlete/activities").mock(return_value=httpx.Response(200, json=[
        strava_activity(1), strava_activity(2, "Run"), strava_activity(3, "GravelRide", "2026-10-03T08:00:00Z", watts=False),
    ], headers={"x-ratelimit-usage": "3,10", "x-ratelimit-limit": "100,1000"}))
    secs = list(range(3600))
    respx.get(f"{API}/activities/1/streams").mock(return_value=httpx.Response(200, json={
        "time": {"data": secs}, "watts": {"data": [250] * 3600}, "heartrate": {"data": [140] * 3600}}))
    respx.get(f"{API}/activities/3/streams").mock(return_value=httpx.Response(200, json={
        "time": {"data": secs}, "heartrate": {"data": [150] * 3600}}))

    cfg = Config(lthr=165)
    db.set("strava_token", {"refresh_token": "r1"})
    client = StravaClient(db, "id", "secret")
    result = await sync(db, client, cfg)

    assert result["listed"] == 2 and result["new"] == 2 and result["streams"] == 2
    assert db.get("strava_token")["refresh_token"] == "r2"

    zwift = db.activity(1)
    assert zwift["np"] == pytest.approx(250)
    assert zwift["mmp"][1200] == 250
    assert zwift["ftp_used"] == 250        # eFTP = 60-min best
    assert zwift["tss"] == pytest.approx(100)
    assert zwift["tss_source"] == "power"

    gravel = db.activity(3)
    assert gravel["tss_source"] == "hr"
    assert gravel["tss"] == pytest.approx((140 / 165) ** 2 * 100, abs=0.1)  # summary avg HR
    assert db.activity(2) is None          # runs are skipped


@respx.mock
async def test_exchange_code_from_pasted_url(db):
    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={
        "access_token": "a", "refresh_token": "b", "expires_at": int(time.time()) + 3600,
        "athlete": {"id": 9, "firstname": "Johan", "lastname": "L"}}))
    client = StravaClient(db, "id", "s")
    assert not client.configured
    athlete = await client.exchange_code(
        "http://localhost/strava-callback?state=&code=abc123&scope=read,activity:read_all,profile:read_all")
    assert athlete["firstname"] == "Johan"
    assert b"code=abc123" in route.calls[0].request.content
    assert client.configured
    assert db.get("strava_athlete")["id"] == 9


async def test_exchange_code_rejects_missing_scope_and_errors(db):
    client = StravaClient(db, "id", "s")
    with pytest.raises(ValueError, match="not granted"):
        await client.exchange_code("http://localhost/strava-callback?code=x&scope=read")
    with pytest.raises(ValueError, match="access_denied"):
        await client.exchange_code("http://localhost/strava-callback?error=access_denied")


@respx.mock
async def test_rate_limit_stops_stream_fetch(db):
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={
        "access_token": "a", "refresh_token": "b", "expires_at": int(time.time()) + 3600}))
    respx.get(f"{API}/athlete/activities").mock(return_value=httpx.Response(200, json=[
        strava_activity(i, start=f"2026-10-0{i}T10:00:00Z") for i in range(1, 4)],
        headers={"x-ratelimit-usage": "96,10", "x-ratelimit-limit": "100,1000"}))
    db.set("strava_token", {"refresh_token": "r1"})
    result = await sync(db, StravaClient(db, "id", "s"), Config())
    assert result["streams"] == 0 and result["pending_streams"] == 3
    # Without streams, Strava's weighted average watts drives TSS
    assert db.activity(1)["tss_source"] == "power"
