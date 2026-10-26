import base64
import json
import re
import time
from datetime import date, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from app import coaches, main, mywhoosh
from app.db import DB
from app.mywhoosh import LOGIN_URL, UPLOAD_URL, MyWhooshClient, MyWhooshError, to_steps, to_workout, workout_id

from .test_workouts import STEPS, step

DISABLED = re.compile(r'value="mywhoosh"[^>]*\sdisabled')  # the MyWhoosh choice greyed out on Profile


def jwt(exp: float) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJIUzI1NiJ9.{payload}.sig"


class FakeMyWhoosh:
    def __init__(self, login=None, upload=None):
        self.calls = []
        self.login = login or {"Success": True, "WhooshId": "w-42", "AccessToken": jwt(time.time() + 86400),
                               "RefreshToken": "r"}
        self.upload = upload or (200, {"message": "Custom workout is being uploaded.", "status": True})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        self.calls.append((str(request.url), request.headers.get("Authorization"), body))
        if str(request.url) == LOGIN_URL:
            return httpx.Response(200 if self.login.get("Success") else 400, json=self.login)
        return httpx.Response(self.upload[0], json=self.upload[1])


def client(db, fake):
    return MyWhooshClient(db, httpx.AsyncClient(transport=httpx.MockTransport(fake)))


@pytest.fixture
def db(tmp_path):
    return DB(tmp_path / "t.db")


def test_steps_map_onto_mywhoosh_with_messages():
    steps = [step("warmup", duration_s=600, power_low=0.5, power_high=0.75, text="Easy start"),
             step("intervals", repeat=3, on_s=300, off_s=120, on_power=1.05, off_power=0.5, cadence=95,
                  cues=[{"offset_s": 450, "text": "Second rep: stay smooth"}]),
             step("steady", duration_s=300, power_low=0.6, power_high=0.6),
             step("cooldown", duration_s=300, power_low=0.6, power_high=0.4)]
    out = to_steps(steps, ("Hold it",), "Last one")
    assert [s["StepType"] for s in out] == ["E_WarmUp"] + ["E_Normal"] * 6 + ["E_Normal", "E_CoolDown"]
    assert (out[0]["StartPower"], out[0]["EndPower"], out[0]["Power"]) == (0.5, 0.75, 0)
    assert (out[-1]["StartPower"], out[-1]["EndPower"]) == (0.6, 0.4)  # our cooldown ramps from power_low
    reps = out[1:7]
    assert [s["Time"] for s in reps] == [300, 120] * 3 and {s["IntervalId"] for s in reps} == {1}
    assert reps[0]["Power"] == 1.05 and reps[0]["Rpm"] == 95 and out[7]["IntervalId"] == 0
    assert [s["Id"] for s in out] == list(range(1, 10))
    assert out[0]["WorkoutMessage"] == [{"Id": 1, "Time": 0, "Message": "Easy start"}]
    # Rep 2 opens with its counter and cheer; the coach's cue at 450 s into the block lands 30 s into it.
    # Rep 3 gets the last-rep cheer. The same events as the .zwo, timed within MyWhoosh's steps.
    assert reps[2]["WorkoutMessage"] == [{"Id": 1, "Time": 0, "Message": "Rep 2/3: Hold it"},
                                         {"Id": 2, "Time": 30, "Message": "Second rep: stay smooth"}]
    assert reps[4]["WorkoutMessage"][0]["Message"] == "Rep 3/3: Last one"


def test_workout_shape_and_stable_id():
    day = {"steps": STEPS, "description": "VO2", "intensity": "vo2max", "target_tss": 55, "test_protocol": "none"}
    w = to_workout("2026-10-27", day, "Tue 27 Oct · VO2 5x3", whoosh_id="w-42")
    assert w["Id"] == workout_id("2026-10-27", "w-42")  # the same account and day always get the same Id
    assert workout_id("2026-10-27", "w-42") != workout_id("2026-10-27", "w-43")  # two athletes never share one
    assert workout_id("2026-10-27", "w-42") != workout_id("2026-10-28", "w-42")
    assert all(mywhoosh.ID_BASE <= workout_id("2026-12-31", f"acc-{i}") < 2**53 for i in range(50))
    assert (w["SportsModeType"], w["Mode"], w["Type"], w["ERGMode"], w["TSS"]) == (0, "E_Ride", "E_Custom", "E_ON", 55)
    assert w["Time"] == sum(s["Time"] for s in w["WorkoutStepsArray"]) and w["StepCount"] == len(w["WorkoutStepsArray"])
    assert w["WorkoutSteps"] == {str(s["Id"]): s for s in w["WorkoutStepsArray"]}
    assert to_workout("2026-10-27", day | {"test_protocol": "ramp"}, "x")["ERGMode"] == "E_OFF"  # tests ride free


async def test_connect_stores_the_token_not_the_password(db):
    fake = FakeMyWhoosh()
    acc = await client(db, fake).connect(" rider@example.com ", "hunter2")
    assert acc == {"email": "rider@example.com", "whoosh_id": "w-42"}
    body = fake.calls[0][2]
    assert (body["Username"], body["Password"], body["Platform"], body["Action"]) == ("rider@example.com", "hunter2", "Android", 1001)
    assert "hunter2" not in json.dumps(db.get("mywhoosh_token")) + json.dumps(db.get("mywhoosh_account"))
    assert MyWhooshClient(db).configured and db.get("mywhoosh_device") == body["DeviceId"]
    with pytest.raises(MyWhooshError, match="Wrong password"):
        await client(DB(":memory:"), FakeMyWhoosh(login={"Success": False, "Message": "Wrong password"})).connect("a", "b")
    # Without a WhooshId every upload would go nowhere: not connected
    with pytest.raises(MyWhooshError):
        await client(DB(":memory:"), FakeMyWhoosh(login={"Success": True, "AccessToken": "t"})).connect("a", "b")

    def listing(request):
        return httpx.Response(200, json=[])  # a JSON list, not an object: an error, not a 500

    with pytest.raises(MyWhooshError):
        await client(DB(":memory:"), listing).connect("a", "b")


async def test_upload_body_and_errors(db):
    fake = FakeMyWhoosh()
    g = client(db, fake)
    await g.connect("rider@example.com", "pw")
    day = {"steps": STEPS, "description": "VO2", "target_tss": 50}
    wid = await g.upload_workout("2026-10-27", day, "Tue 27 Oct · VO2 5x3", ("Go",), "Last")
    url, auth, body = fake.calls[-1]
    assert url == UPLOAD_URL and auth.startswith("Bearer ") and wid == workout_id("2026-10-27", "w-42")
    assert body["UserId"] == "w-42" and body["SportsModeType"] == 0 and isinstance(body["SportsModeType"], int)
    assert body["WorkoutsData"][0]["Id"] == wid and body["WorkoutsData"][0]["SportsModeType"] == 0
    fake.upload = (200, {"message": "You don't have enough credit!", "status": False})
    with pytest.raises(MyWhooshError, match="no room for more custom workouts"):
        await g.upload_workout("2026-10-27", day, "x")
    # Anything but an explicit yes isn't "sent": a list, the login's own {"Success": false}, a page, a bare object
    for reply in ([], {"Success": False, "Message": "Maintenance"}, {"message": "Custom workout is being uploaded."}):
        fake.upload = (200, reply)
        with pytest.raises(MyWhooshError, match="refused"):
            await g.upload_workout("2026-10-27", day, "x")
    fake.upload = (200, {"Success": True})
    await g.upload_workout("2026-10-27", day, "x")
    fake.upload = (401, {"message": "Unauthorized"})
    with pytest.raises(MyWhooshError, match="expired"):
        await g.upload_workout("2026-10-27", day, "x")
    assert not g.configured  # shows as signed out on Profile
    expired = DB(":memory:")
    expired.set("mywhoosh_token", {"access_token": jwt(time.time() - 10), "whoosh_id": "w", "expires_at": time.time() - 10})
    assert not MyWhooshClient(expired).configured


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    with TestClient(main.app) as c:
        st = main.app.state.st
        st.db.set("zwift_refreshed", date.today().isoformat())  # keep the scheduler off the network
        day = (date.today() + timedelta(days=1)).isoformat()
        payload = {"week_focus": "f", "rationale": "r", "block_week": 1, "days": [
            {"date": day, "kind": "zwift", "title": "VO2 5x3", "description": "d", "duration_min": 40,
             "intensity": "vo2max", "target_tss": 50, "bike": "trainer", "terrain": "", "steps": STEPS}]}
        c.plan_id = st.db.save_plan(day, "2026-10-12T08:00:00", "m", payload, [], "carmichael")
        c.day, c.st = day, st
        yield c


def test_pick_mywhoosh_connect_and_send(web, monkeypatch):
    st = web.st
    html = web.get("/profile").text
    # Both sign-ins show at all times; MyWhoosh can't be picked until it's connected
    assert "Indoor app" in html and "Connect Zwift" in html and "Connect MyWhoosh" in html
    assert DISABLED.search(html) and "Connection needs to be created" in html
    assert 'href="#conn-mywhoosh"' in html
    r = web.post("/profile/indoor", data={"workout_app": "mywhoosh"})
    assert "Connect MyWhoosh first" in r.text and st.cfg.workout_app == "zwift"  # the server refuses it too
    assert web.post("/profile/indoor", data={"workout_app": "rouvy"}).status_code == 400

    async def connect(self, email, password):
        self.db.set("mywhoosh_token", {"access_token": "t", "whoosh_id": "w", "expires_at": time.time() + 3600})
        self.db.set("mywhoosh_account", {"email": email, "whoosh_id": "w"})
        return {"email": email, "whoosh_id": "w"}

    sent = {}

    async def upload(self, day, plan_day, name, cheers=(), last_rep=""):
        sent.update(day=day, name=name, last_rep=last_rep)
        return workout_id(day, "w")

    monkeypatch.setattr(MyWhooshClient, "connect", connect)
    monkeypatch.setattr(MyWhooshClient, "upload_workout", upload)
    r = web.post("/profile/mywhoosh/connect", data={"mywhoosh_email": "rider@example.com", "mywhoosh_password": "pw"})
    assert "Connected to MyWhoosh as rider@example.com" in r.text and "pw" not in st.db.conn.execute(
        "SELECT group_concat(value) FROM kv").fetchone()[0]
    assert not DISABLED.search(r.text)  # connected: now it can be picked
    r = web.post("/profile/indoor", data={"workout_app": "mywhoosh"})
    assert "Indoor sessions go to MyWhoosh now" in r.text and st.cfg.workout_app == "mywhoosh"
    plan = web.get(f"/plan?week={web.day}").text
    assert f'action="/plan/{web.plan_id}/{web.day}/mywhoosh"' in plan and "Send to MyWhoosh" in plan
    r = web.post(f"/plan/{web.plan_id}/{web.day}/mywhoosh")
    assert "is in MyWhoosh under My Workouts" in r.text and "In MyWhoosh since" in r.text and "Send again" in r.text
    assert sent["day"] == web.day and sent["last_rep"] == coaches.get("carmichael").last_rep and "VO2 5x3" in sent["name"]
    r = web.post("/profile/mywhoosh/disconnect")
    assert not st.mywhoosh.configured and "login has expired" not in r.text  # disconnected, not expired


def test_a_stale_choice_falls_back_instead_of_breaking_pages(web):
    st = web.st
    st.db.set("settings", (st.db.get("settings") or {}) | {"workout_app": "rouvy", "activity_source": "polar"})
    st.reload()
    assert (st.cfg.workout_app, st.cfg.activity_source) == ("zwift", "strava")
    assert web.get("/").status_code == 200 and web.get("/profile").status_code == 200


def test_upload_failure_is_flashed(web, monkeypatch):
    st = web.st
    st.cfg.workout_app = "mywhoosh"
    st.save()
    st.reload()
    st.db.set("mywhoosh_token", {"access_token": "t", "whoosh_id": "w", "expires_at": time.time() + 3600})

    async def boom(self, *a, **k):
        raise MyWhooshError("MyWhoosh has no room for more custom workouts")

    monkeypatch.setattr(MyWhooshClient, "upload_workout", boom)
    assert "Couldn" in web.post(f"/plan/{web.plan_id}/{web.day}/mywhoosh").text
    assert mywhoosh.ID_BASE < workout_id("2026-01-01")
