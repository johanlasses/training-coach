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
    respx.get(f"{API}/activities/2/streams").mock(return_value=httpx.Response(200, json={  # a Stryd: running power
        "time": {"data": secs}, "watts": {"data": [300] * 3600}, "heartrate": {"data": [150] * 3600}}))
    respx.get(f"{API}/activities/3/streams").mock(return_value=httpx.Response(200, json={
        "time": {"data": secs}, "heartrate": {"data": [150] * 3600}}))

    cfg = Config(lthr=165)
    db.set("strava_token", {"refresh_token": "r1"})
    client = StravaClient(db, "id", "secret")
    result = await sync(db, client, cfg)

    assert result["listed"] == 3 and result["new"] == 3 and result["streams"] == 3
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
    run = db.activity(2)                   # runs are kept, scored on HR, and their power never counts
    assert (run["tss_source"], run["np"], run["mmp"], run["ftp_used"]) == ("rhr", None, None, None)
    assert run["spark_hr"] and not run["spark_watts"]


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
async def test_stream_404_is_skipped_and_rides_still_scored(db):
    # Seen live in 0.4.0: a manual activity's streams 404'd, aborting every sync before recompute(), which left
    # streamed rides with ftp_used NULL and made the dashboard 500.
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={
        "access_token": "a", "refresh_token": "b", "expires_at": int(time.time()) + 3600}))
    respx.get(f"{API}/athlete/activities").mock(return_value=httpx.Response(200, json=[
        strava_activity(1, start="2026-10-01T10:00:00Z"), strava_activity(2, start="2026-10-02T10:00:00Z")]))
    respx.get(f"{API}/activities/2/streams").mock(return_value=httpx.Response(404, json={"message": "Record Not Found"}))
    respx.get(f"{API}/activities/1/streams").mock(return_value=httpx.Response(200, json={
        "time": {"data": list(range(3600))}, "watts": {"data": [250] * 3600}}))
    db.set("strava_token", {"refresh_token": "r1"})
    result = await sync(db, StravaClient(db, "id", "s"), Config())
    assert result["streams"] == 2 and result["pending_streams"] == 0
    assert db.activity(1)["power_hist"] and db.activity(1)["ftp_used"] == 250
    assert db.activity(2)["streams_fetched"] == 1 and db.activity(2)["ftp_used"]


@respx.mock
async def test_stream_server_error_still_scores_stored_rides(db):
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={
        "access_token": "a", "refresh_token": "b", "expires_at": int(time.time()) + 3600}))
    respx.get(f"{API}/athlete/activities").mock(return_value=httpx.Response(200, json=[
        strava_activity(1, start="2026-10-01T10:00:00Z"), strava_activity(2, start="2026-10-02T10:00:00Z")]))
    respx.get(f"{API}/activities/2/streams").mock(return_value=httpx.Response(200, json={
        "time": {"data": list(range(3600))}, "watts": {"data": [250] * 3600}}))
    respx.get(f"{API}/activities/1/streams").mock(return_value=httpx.Response(502))
    db.set("strava_token", {"refresh_token": "r1"})
    with pytest.raises(httpx.HTTPStatusError):
        await sync(db, StravaClient(db, "id", "s"), Config())
    assert db.activity(2)["ftp_used"] == 250
    assert db.activity(1)["streams_fetched"] == 0  # transient: retried next sync


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


@respx.mock
async def test_429_pauses_strava_and_sync_does_not_fail(db):
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={
        "access_token": "a", "refresh_token": "b", "expires_at": int(time.time()) + 3600}))
    listing = respx.get(f"{API}/athlete/activities").mock(return_value=httpx.Response(
        429, headers={"x-readratelimit-usage": "40,1003", "x-readratelimit-limit": "100,1000"}))
    db.set("strava_token", {"refresh_token": "r1"})
    db.set("last_sync", {"listed": 5, "pending_streams": 2})
    result = await sync(db, StravaClient(db, "id", "s"), Config())
    assert "resumes at 00:00 UTC" in result["rate_limited"]  # daily limit: wait for midnight UTC
    assert result["listed"] == 5 and db.get("strava_backoff_until") > time.time()
    # A fresh client (e.g. after saving settings) respects the pause without calling Strava
    await sync(db, StravaClient(db, "id", "s"), Config())
    assert listing.call_count == 1


@respx.mock
async def test_backfill_keeps_daily_reserve_for_listing(db):
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={
        "access_token": "a", "refresh_token": "b", "expires_at": int(time.time()) + 3600}))
    respx.get(f"{API}/athlete/activities").mock(return_value=httpx.Response(200, json=[
        strava_activity(i, start=f"2026-10-0{i}T10:00:00Z") for i in range(1, 4)],
        headers={"x-readratelimit-usage": "10,905", "x-readratelimit-limit": "100,1000"}))
    streams = respx.get(url__regex=rf"{API}/activities/\d+/streams")
    db.set("strava_token", {"refresh_token": "r1"})
    result = await sync(db, StravaClient(db, "id", "s"), Config())
    assert result["streams"] == 0 and result["pending_streams"] == 3 and not result["rate_limited"]
    assert streams.call_count == 0


def test_next_reset():
    from datetime import datetime, timezone

    from app.strava import _next_reset
    now = datetime(2026, 10, 12, 14, 7, 30, tzinfo=timezone.utc)
    assert datetime.fromtimestamp(_next_reset(now, daily=False), timezone.utc).strftime("%H:%M") == "14:15"
    assert datetime.fromtimestamp(_next_reset(now, daily=True), timezone.utc).isoformat() == "2026-10-13T00:00:00+00:00"


def test_sync_now_during_pause_explains_instead_of_failing(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app import main
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    with TestClient(main.app) as c:
        c.app.state.st.db.set("strava_backoff_until", time.time() + 600)
        assert "Sync resumes on its own" in c.post("/sync").text
        assert "sync" not in c.app.state.st.jobs
