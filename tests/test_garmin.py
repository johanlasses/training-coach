import base64
import json
import time
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from app import garmin, health, main
from app.config import Config
from app.db import DB
from app.garmin import BASE, TOKEN_PATH, GarminClient, GarminError, fit_streams, parse_token, summary_from_garmin
from app.strava import RateLimited
from app.sync import sync

from .fitfile import fit_records, zipped


def activity(i, key="virtual_ride", start="2026-10-20 06:00:00", power=True, minutes=60):
    return {"activityId": i, "activityName": f"Ride {i}", "activityType": {"typeKey": key, "typeId": 1, "parentTypeId": 2},
            "startTimeLocal": start, "startTimeGMT": start, "distance": 30000.0, "duration": minutes * 60.0,
            "movingDuration": minutes * 60.0, "elapsedDuration": minutes * 60.0 + 30, "elevationGain": 210.0,
            "avgPower": 200.0 if power else None, "normPower": 215.0 if power else None, "maxPower": 600.0,
            "averageHR": 140.0, "maxHR": 170.0, "averageSpeed": 8.3}


class FakeGarmin:
    """Answers like the Personal API: token, activity search (paged), FIT download and the health endpoints."""

    def __init__(self, acts=(), fit=b"", token_status=200, token_body=None):
        self.acts, self.fit, self.calls = list(acts), fit, []
        self.token_status, self.token_body = token_status, token_body or {"access_token": "t1", "expires_in": 86400}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, q = request.url.path, dict(request.url.params)
        self.calls.append((request.method, path, q, request.headers.get("Authorization")))
        if path == TOKEN_PATH:
            assert request.headers["Authorization"] == "ApiKey key-1"
            return httpx.Response(self.token_status, json=self.token_body)
        assert request.headers["Authorization"] == "Bearer t1"
        if path.endswith("/search/activities"):
            start, limit = int(q.get("start", 0)), int(q.get("limit", 99))
            return httpx.Response(200, json=self.acts[start:start + limit])
        if path.startswith("/download-service/files/activity/"):
            return httpx.Response(200, content=self.fit)
        if path.startswith("/hrv-service/hrv/daily/"):
            a, b = path.split("/")[-2:]
            return httpx.Response(200, json={"hrvSummaries": [{"calendarDate": d, "lastNightAvg": 55} for d in days(a, b)]})
        if path.endswith("/dailySleepSummariesByDate"):
            return httpx.Response(200, json=[{"calendarDate": d, "sleepTimeSeconds": 7.5 * 3600} for d in days(q["startDate"], q["endDate"])])
        if path.endswith("/v2/dailysummaries"):  # live, these came without heartRate: every other day here
            return httpx.Response(200, json={"data": [{"calendarDate": d} | ({"heartRate": {"restingValue": 48}} if i % 2 else {})
                                                      for i, d in enumerate(days(q["startDate"], q["endDate"]))]})
        if path.endswith("/wellness/dailyHeartRate"):
            return httpx.Response(200, json={"calendarDate": q["date"], "restingHeartRate": 49})
        if path.startswith("/weight-service/weight/all/"):
            a, b = path.split("/")[-2:]
            return httpx.Response(200, json=[{"calendarDate": b, "weight": 74.62}])
        return httpx.Response(404)


def days(a, b):
    d, out = date.fromisoformat(a), []
    while d <= date.fromisoformat(b):
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def client(db, fake, key="key-1"):
    return GarminClient(db, key, httpx.AsyncClient(transport=httpx.MockTransport(fake), base_url=BASE))


@pytest.fixture
def db(tmp_path):
    return DB(tmp_path / "t.db")


def test_summary_maps_onto_stravas_sport_types():
    s = summary_from_garmin(activity(7, "virtual_ride", "2026-10-20 18:05:00"))
    assert (s["id"], s["sport_type"], s["trainer"], s["start_date"], s["start_date_local"]) == (
        7, "VirtualRide", 1, "2026-10-20T18:05:00Z", "2026-10-20T18:05:00")
    assert (s["moving_time"], s["weighted_avg_watts"], s["device_watts"], s["kilojoules"]) == (3600, 215.0, 1, 720.0)
    assert summary_from_garmin(activity(8, "gravel_cycling"))["sport_type"] == "GravelRide"
    assert summary_from_garmin(activity(9, "indoor_cycling"))["trainer"] == 1
    walk = summary_from_garmin(activity(10, "walking", power=False))
    assert walk["sport_type"] == "Other" and walk["device_watts"] == 0 and walk["kilojoules"] is None


def test_fit_streams_read_power_and_heart_rate_from_the_zipped_download():
    fit = fit_records(datetime(2026, 10, 20, 6, tzinfo=timezone.utc), [200, 210, 0], [130, 131, 0])
    assert fit_streams(zipped(fit)) == {"time": [0, 1, 2], "watts": [200, 210, 0], "heartrate": [130, 131, 0]}
    assert fit_streams(fit)["time"] == [0, 1, 2]  # a bare .fit works too
    assert fit_streams(zipped(b"", "notes.txt")) == {}


def test_parse_token_accepts_the_usual_shapes():
    now = time.time()
    tok, exp = parse_token(httpx.Response(200, json={"access_token": "a", "expires_in": 3600}))
    assert tok == "a" and now + 3500 < exp < now + 3700
    tok, exp = parse_token(httpx.Response(200, json={"accessToken": "b", "expiresAt": int((now + 7200) * 1000)}))
    assert tok == "b" and now + 7100 < exp < now + 7300
    tok, exp = parse_token(httpx.Response(200, text="plain-token"))
    assert tok == "plain-token" and exp > now + 23 * 3600  # Garmin: valid 24 h
    # What Garmin really sends (Oct 2026): the bare JWT, whose exp claim is used
    payload = base64.urlsafe_b64encode(json.dumps({"exp": 1_793_047_590, "scope": ["CONNECT_READ"]}).encode()).decode().rstrip("=")
    tok, exp = parse_token(httpx.Response(200, text=f"eyJhbGciOiJSUzI1NiJ9.{payload}.sig"))
    assert tok.endswith(".sig") and exp == 1_793_047_590
    with pytest.raises(GarminError, match="fields: error"):
        parse_token(httpx.Response(200, json={"error": "nope"}))


async def test_token_is_cached_and_a_refused_key_shows_as_disconnected(db):
    fake = FakeGarmin()
    g = client(db, fake)
    await g._token()
    await g._token()
    tokens = lambda: len([c for c in fake.calls if c[1] == TOKEN_PATH])  # noqa: E731
    assert tokens() == 1 and g.configured  # cached for its lifetime
    await g.check()
    assert tokens() == 2  # "Check again" asks Garmin, not the cache
    refused = FakeGarmin(token_status=401)
    g = client(DB(":memory:"), refused)
    with pytest.raises(GarminError, match="expired"):
        await g.check()
    assert not g.configured and g.status()["refused"]
    # An outage (Cloudflare 403, maintenance 404, a 200 HTML page) never marks the key refused
    for fake in (FakeGarmin(token_status=403), FakeGarmin(token_status=404),
                 lambda r: httpx.Response(200, text="<html>Just a moment...</html>")):
        g = client(DB(":memory:"), fake)
        with pytest.raises(GarminError, match="next sync"):
            await g.check()
        assert g.configured and not g.status().get("refused")


async def test_rate_limit_is_rate_limited(db):
    g = client(db, lambda r: httpx.Response(429))
    with pytest.raises(RateLimited):
        await g.check()


async def test_sync_from_garmin_imports_rides_only_with_streams(db):
    fit = fit_records(datetime(2026, 10, 20, 6, tzinfo=timezone.utc), [250] * 3600, [140] * 3600)
    first = datetime(2026, 6, 1, 6)
    acts = [activity(i, start=(first + timedelta(days=i)).strftime("%Y-%m-%d %H:%M:%S")) for i in range(1, 121)] + [
        activity(500, "walking", power=False)]
    fake = FakeGarmin(acts, zipped(fit))
    result = await sync(db, client(db, fake), Config(lthr=165))
    assert result["listed"] == 120 and len(db.activities()) == 120  # the walk stays out until 0.11
    pages = [c[2]["start"] for c in fake.calls if c[1].endswith("/search/activities")]
    # Until an empty page (Garmin's max page size isn't documented): 100, 21, then nothing
    assert pages == ["0", "100", "121"] and all("activityType" not in c[2] for c in fake.calls)
    streamed = [a for a in db.activities() if a["streams_fetched"]]
    assert len(streamed) == 40 and result["pending_streams"] == 80  # capped per run, like Strava
    assert round(streamed[0]["np"]) == 250 and streamed[0]["tss_source"] == "power"


async def test_an_unreadable_download_gives_no_streams_instead_of_stopping_sync(db):
    acts = [activity(1, start="2026-10-20 06:00:00")]
    fake = FakeGarmin(acts, b"<html>Service unavailable</html>")
    result = await sync(db, client(db, fake), Config())
    a = db.activity(1)
    assert a["streams_fetched"] == 1 and a["np"] is None and result["pending_streams"] == 0
    assert a["tss_source"] == "power"  # scored from Garmin's own normPower instead


async def test_one_failing_health_metric_keeps_the_others(db):
    fake = FakeGarmin()

    def no_weight(request):
        if request.url.path.startswith("/weight-service/"):
            return httpx.Response(204)  # no scale: Garmin's "no data"
        if request.url.path.endswith("/dailySleepSummariesByDate"):
            return httpx.Response(500)
        return fake(request)

    out = await client(db, no_weight).health_days(date(2026, 10, 20), date(2026, 10, 21))
    assert out["2026-10-21"].keys() == {"hrv", "rhr"}  # sleep failed, weight had no data, the rest came in


def test_e_bikes_and_hand_cycles_arent_rides():
    from app.strava import RIDE_TYPES
    for key in ("e_bike_fitness", "e_bike_mountain", "hand_cycling", "bmx"):
        assert summary_from_garmin(activity(1, key))["sport_type"] not in RIDE_TYPES
    s = summary_from_garmin(activity(1, "indoor_cycling") | {"movingDuration": 600.0})  # no speed sensor indoors
    assert s["moving_time"] == 3600 and s["kilojoules"] == 720.0


async def test_garmin_health_backfills_then_rechecks(db):
    fake = FakeGarmin()
    today = date(2026, 10, 25)
    n = await health.fetch_garmin(db, client(db, fake), today)
    rows = db.health("2000-01-01")
    assert {r["date"] for r in rows if r["metric"] == "hrv"} == set(days((today - timedelta(days=34)).isoformat(), today.isoformat()))
    assert n == 35 * 3 + 2  # hrv, sleep, rhr every day; one weight per chunk
    by = {(r["date"], r["metric"]): r["value"] for r in rows}
    assert by[(today.isoformat(), "sleep")] == 7.5 and by[(today.isoformat(), "weight")] == 74.62
    rhr = {d: v for (d, m), v in by.items() if m == "rhr"}
    assert len(rhr) == 35 and set(rhr.values()) == {48, 49}  # the summary's value, else the per-day endpoint's
    per_day = [c for c in fake.calls if c[1].endswith("/wellness/dailyHeartRate")]
    assert 0 < len(per_day) < 35  # only for the days the summaries left out
    fake.calls.clear()
    await health.fetch_garmin(db, client(db, fake), today)
    hrv = next(c for c in fake.calls if c[1].startswith("/hrv-service"))
    assert hrv[1].endswith(f"/{(today - timedelta(days=2)).isoformat()}/{today.isoformat()}")  # only the last 3 days
    assert health.enabled(Config(health_source="garmin", garmin_api_key="k"))
    assert not health.enabled(Config(health_source="garmin")) and not health.enabled(Config())


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    with TestClient(main.app) as c:
        yield c


def test_profile_connections_and_the_key_never_rendered(web, monkeypatch):
    st = main.app.state.st
    html = web.get("/profile").text
    assert 'id="connections"' in html and "Strava" in html and 'name="garmin_api_key"' in html  # connect before switching
    r = web.post("/profile/source", data={"activity_source": "garmin"})
    assert "Connect Garmin first" in r.text and st.cfg.activity_source == "strava"  # nothing cleared without a key

    checks = []

    async def check(self):
        checks.append(self.api_key)

    started = []
    monkeypatch.setattr(garmin.GarminClient, "check", check)
    monkeypatch.setattr(st, "start_job", lambda name, coro: (coro.close(), started.append(name)))
    r = web.post("/profile/garmin", data={"garmin_api_key": "super-secret-key"})
    assert "Garmin connected" in r.text and "super-secret-key" not in r.text and started == []  # rides still from Strava
    assert st.cfg.garmin_api_key == "super-secret-key" and "super-secret-key" not in web.get("/settings").text
    assert json.loads(st.db.conn.execute("SELECT value FROM kv WHERE key='settings'").fetchone()[0])["garmin_api_key"]
    assert "Check again" in web.get("/profile").text
    web.post("/profile/garmin", data={})  # Check again: the stored key
    assert checks == ["super-secret-key", "super-secret-key"]
    web.post("/profile/source", data={"activity_source": "garmin"})
    assert st.cfg.activity_source == "garmin" and st.source.configured and started == ["sync"]
    # A crafted form can't switch a source past the clearing in /profile/source
    web.post("/profile", data={"activity_source": "strava", "goals": "x"})
    assert st.cfg.activity_source == "garmin" and st.cfg.goals == "x"
    web.post("/profile/garmin/disconnect")
    assert st.cfg.garmin_api_key == "" and not st.garmin.configured
    assert "Garmin Connect is not connected" in web.get("/").text


def test_connecting_strava_while_rides_come_from_garmin_doesnt_sync_garmin(web, monkeypatch):
    st = main.app.state.st
    st.cfg.activity_source = "garmin"
    st.save()
    st.reload()
    started = []

    async def exchange(self, pasted):
        self.db.set("strava_token", {"refresh_token": "r"})
        return {"firstname": "Dev", "lastname": "Rider"}

    monkeypatch.setattr(main.StravaClient, "exchange_code", exchange)
    monkeypatch.setattr(st, "start_job", lambda name, coro: (coro.close(), started.append(name)))
    r = web.post("/settings/strava/connect", data={"redirect_url": "http://localhost/strava-callback?code=x"})
    assert "Connected to Strava as Dev Rider. Pick Strava under Rides" in r.text and "First sync" not in r.text
    assert started == []  # no sync of the Garmin source behind the Strava success message


async def test_a_garmin_429_on_connect_is_a_message_not_a_500(web, monkeypatch):
    async def busy(self):
        raise RateLimited("busy")

    monkeypatch.setattr(garmin.GarminClient, "check", busy)
    r = web.post("/profile/garmin", data={"garmin_api_key": "k"})
    assert r.status_code == 200 and "Garmin is busy" in r.text


def test_switching_the_source_clears_what_the_old_one_brought(web, monkeypatch):
    st = main.app.state.st
    started = []
    monkeypatch.setattr(st, "start_job", lambda name, coro: (coro.close(), started.append(name)))  # never the real Garmin
    st.db.upsert_activity_summary({"id": 1, "name": "Old", "sport_type": "Ride", "start_date": "2026-10-20T06:00:00Z",
                                   "start_date_local": "2026-10-20T08:00:00", "moving_time": 3600})
    st.db.set_health("2026-10-20", "hrv", 60, "ms")
    st.db.set("last_activity_start", "2026-10-20T06:00:00Z")
    st.cfg.garmin_api_key = "k"
    st.save()
    st.reload()
    web.post("/profile/source", data={"activity_source": "strava", "health_source": "apple"})  # no change: nothing cleared
    assert st.db.activities() and st.db.health("2000-01-01")
    st.jobs["sync"] = {"running": True}
    r = web.post("/profile/source", data={"activity_source": "garmin"})
    assert "A sync is running" in r.text and st.db.activities()  # it would write Strava rides into the cleared table
    st.jobs["sync"] = {"running": False}
    r = web.post("/profile/source", data={"activity_source": "garmin"})
    assert "Rides now come from Garmin Connect" in r.text and not st.db.activities() and st.db.get("last_activity_start") is None
    assert st.db.get("mark_notified") and st.db.health("2000-01-01")  # health stays: its source didn't change
    web.post("/profile/source", data={"health_source": "garmin"})
    assert not st.db.health("2000-01-01") and st.cfg.health_source == "garmin"
    assert web.post("/profile/source", data={"activity_source": "fitbit"}).status_code == 400


async def test_first_sync_after_a_switch_sends_no_second_round_of_pushes(db):
    db.set("mark_notified", True)
    fake = FakeGarmin([activity(1, start=datetime.now().strftime("%Y-%m-%d 06:00:00"))], b"")
    await sync(db, client(db, fake), Config())
    assert db.activity(1)["notified"] == 1 and db.get("mark_notified") is None
