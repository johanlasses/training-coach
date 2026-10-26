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
async def test_exchange_code_says_when_the_strava_app_is_full(db):
    # A new Strava API app connects only its owner: a second athlete gets a 403, and "connect again" won't help
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(403, json={
        "message": "Limit of connected athletes exceeded", "errors": []}))
    with pytest.raises(ValueError, match="limit of connected athletes.*your own free Strava API app"):
        await StravaClient(db, "id", "s").exchange_code("http://localhost/strava-callback?code=x&scope=read,activity:read_all")
    assert db.get("strava_token") is None


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


def recording(i, sport, start, minutes, **kw):
    return {"id": i, "name": f"{sport} {i}", "sport_type": sport, "start_date": start, "start_date_local": start.rstrip("Z"),
            "moving_time": minutes * 60, "elapsed_time": minutes * 60 + 60, "trainer": int(sport == "VirtualRide"),
            "device_watts": 1, "avg_hr": 140, "distance": 20000} | kw


def test_zwift_and_the_head_unit_recording_one_ride_count_once(db):
    from app.sync import find_duplicates, mark_duplicates, recompute
    db.upsert_activity_summary(recording(1, "Ride", "2026-10-20T16:58:00Z", 62, trainer=1))      # the Garmin on the bars
    db.upsert_activity_summary(recording(2, "VirtualRide", "2026-10-20T17:00:00Z", 60))         # Zwift
    db.upsert_activity_summary(recording(3, "Run", "2026-10-20T17:30:00Z", 30))                 # another sport: not one
    db.upsert_activity_summary(recording(4, "VirtualRide", "2026-10-21T17:00:00Z", 60))         # the next day
    db.upsert_activity_summary(recording(5, "Ride", "2026-10-21T18:05:00Z", 60))                # right after: not one
    assert find_duplicates(db.activities(duplicates=True)) == {1: 2}  # Zwift's recording is the one kept
    recompute(db, Config())
    assert [a["id"] for a in db.activities()] == [5, 4, 3, 2]  # load, lists and views see the session once
    assert db.activity(1)["duplicate_of"] == 2
    assert 1 not in {a["id"] for a in db.activities_missing_streams(10)}  # no stream call for the second copy
    # Two devices on one outdoor ride: the one with power stays
    db.upsert_activity_summary(recording(6, "Ride", "2026-10-22T08:00:00Z", 120, device_watts=0))
    db.upsert_activity_summary(recording(7, "Ride", "2026-10-22T08:01:00Z", 119))
    assert mark_duplicates(db) == {1: 2, 6: 7}


def test_a_second_recording_arriving_later_gets_no_second_push(db):
    from app.sync import mark_duplicates
    db.upsert_activity_summary(recording(1, "Ride", "2026-10-20T16:58:00Z", 62, trainer=1))
    db.update_activity(1, notified=1, feedback={"headline": "Solid", "notification": "n", "detail": "d"})
    db.upsert_activity_summary(recording(2, "VirtualRide", "2026-10-20T17:00:00Z", 60))  # Zwift's upload, an hour later
    mark_duplicates(db)
    keep = db.activity(2)
    assert keep["notified"] == 1 and keep["feedback"]["headline"] == "Solid"
    assert not db.activities_to_notify("2026-10-20T00:00:00Z")


def test_deleting_the_kept_copy_on_strava_brings_no_second_push(db):
    from app.sync import mark_duplicates
    db.upsert_activity_summary(recording(1, "Ride", "2026-10-20T16:58:00Z", 62, trainer=1))
    db.upsert_activity_summary(recording(2, "VirtualRide", "2026-10-20T17:00:00Z", 60))
    mark_duplicates(db)
    db.update_activity(2, notified=1, feedback={"headline": "Solid", "notification": "n", "detail": "d"})  # Zwift's pushed
    mark_duplicates(db)
    db.delete_activities_not_in({1}, "2026-10-01T00:00:00Z")  # the athlete deletes Zwift's copy on Strava
    mark_duplicates(db)
    assert [a["id"] for a in db.activities()] == [1] and db.activity(1)["notified"] == 1
    assert not db.activities_to_notify("2026-10-20T00:00:00Z") and db.activity(1)["feedback"]["headline"] == "Solid"


def test_what_isnt_a_duplicate_and_which_copy_is_kept(db):
    from app.sync import find_duplicates
    # A commute saved hours later (stop button forgotten: 10 h elapsed, 40 min moving) doesn't swallow the evening ride
    db.upsert_activity_summary(recording(1, "Ride", "2026-10-20T06:30:00Z", 40, elapsed_time=10 * 3600))
    db.upsert_activity_summary(recording(2, "VirtualRide", "2026-10-20T16:00:00Z", 60))
    # Zwift crashed after 30 min, the watch has the whole 90: the watch's recording is kept
    db.upsert_activity_summary(recording(3, "VirtualRide", "2026-10-21T17:00:00Z", 30, avg_hr=None))
    db.upsert_activity_summary(recording(4, "Ride", "2026-10-21T16:59:00Z", 90, trainer=1, device_watts=0))
    # A ride then a run (a brick) is two sessions
    db.upsert_activity_summary(recording(5, "Ride", "2026-10-22T08:00:00Z", 60))
    db.upsert_activity_summary(recording(6, "Run", "2026-10-22T08:05:00Z", 30))
    assert find_duplicates(db.activities(duplicates=True)) == {3: 4}


def test_a_zwift_file_without_hr_gets_the_watchs(db):
    from app.sync import mark_duplicates
    db.upsert_activity_summary(recording(1, "VirtualRide", "2026-10-20T17:00:00Z", 60, avg_hr=None, max_hr=None))
    db.upsert_activity_summary(recording(2, "Ride", "2026-10-20T17:00:30Z", 60, trainer=1, avg_hr=151, max_hr=175))
    assert mark_duplicates(db) == {2: 1}
    assert (db.activity(1)["avg_hr"], db.activity(1)["max_hr"]) == (151, 175)


def test_a_hidden_copys_page_shows_the_kept_one(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app import main
    from app.sync import mark_duplicates
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    with TestClient(main.app) as c:
        db = main.app.state.st.db
        db.upsert_activity_summary(recording(1, "Ride", "2026-10-20T16:58:00Z", 62, trainer=1))
        db.upsert_activity_summary(recording(2, "VirtualRide", "2026-10-20T17:00:00Z", 60))
        mark_duplicates(db)
        r = c.get("/activity/1", headers={"X-Ingress-Path": "/api/hassio_ingress/abc"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/api/hassio_ingress/abc/activity/2"
