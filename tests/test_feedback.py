import json
import math
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import feedback, ha, llm, main, overview, workouts
from app.config import Config
from app.db import DB

from .test_llm import mock_api, sse

NOW = datetime(2026, 10, 16, 18, 0, tzinfo=timezone.utc)
FB = {"headline": "Sweet spot nailed", "notification": "All three reps on target. Rest tomorrow.", "detail": "Good."}


def step(type_, **kw):
    return {"type": type_, "duration_s": 0, "power_low": 0, "power_high": 0, "repeat": 0, "on_s": 0, "off_s": 0,
            "on_power": 0, "off_power": 0, "cadence": 0, "text": "", "cues": []} | kw


STEPS = [step("warmup", duration_s=600, power_low=0.5, power_high=0.75),
         step("intervals", repeat=3, on_s=600, off_s=300, on_power=0.9, off_power=0.55),
         step("cooldown", duration_s=300, power_low=0.6, power_high=0.4)]


def ride(db, i=1, start=NOW - timedelta(hours=3), streams=True, watts=None):
    db.upsert_activity_summary({"id": i, "name": f"Ride {i}", "sport_type": "VirtualRide",
                                "start_date": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                "start_date_local": start.strftime("%Y-%m-%dT%H:%M:%S"), "distance": 30000,
                                "moving_time": 3600, "elapsed_time": 3600, "trainer": 1, "avg_hr": 140})
    db.update_activity(i, streams_fetched=int(streams), ftp_used=250, tss=60, np=220, spark_watts=watts)
    return db.activity(i)


def plan_today(db, day: date, steps=STEPS):
    from app import stats
    days = [{"date": (stats.week_start(day) + timedelta(days=k)).isoformat(), "kind": "rest", "title": "Rest",
             "description": "", "duration_min": 0, "intensity": "recovery", "target_tss": 0, "bike": "none",
             "terrain": "", "steps": [], "test_protocol": "none"} for k in range(7)]
    today = next(d for d in days if d["date"] == day.isoformat())
    today.update(kind="zwift", title="Sweet spot 3×10", duration_min=60, intensity="sweetspot", target_tss=65, steps=steps)
    db.save_plan(stats.week_start(day).isoformat(), "2026-10-12T08:00:00", "m",
                 {"week_focus": "Build", "rationale": "", "days": days}, [], "carmichael")


@pytest.fixture
def db(tmp_path):
    return DB(tmp_path / "t.db")


@pytest.fixture
def sent(monkeypatch):
    out = []

    async def notify(service, title, message, data=None):
        out.append({"service": service, "title": title, "message": message, "data": data})

    monkeypatch.setattr(ha, "notify", notify)
    return out


def fake_llm(monkeypatch, reply=FB):
    calls = []

    async def run(api_key, system, messages, **kw):
        calls.append({"system": system, "messages": messages, **kw})
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(reply))], model="claude-haiku-5-5")

    monkeypatch.setattr(llm, "run", run)
    return calls


# --- models per purpose ---------------------------------------------------

@pytest.mark.parametrize("purpose, model, fallback", [
    ("plan", "claude-opus-5-5", True), ("analysis", "claude-opus-5-5", True),
    ("chat", "claude-sonnet-5-5", True), ("ride", "claude-haiku-5-5", False)])
async def test_each_purpose_runs_on_its_model(monkeypatch, purpose, model, fallback):
    requests = mock_api(monkeypatch, sse("{}"))
    await llm.run("key", "sys", [{"role": "user", "content": "hi"}], purpose=purpose)
    body = json.loads(requests[0].content)
    assert body["model"] == model
    assert ("fallbacks" in body) is fallback  # Haiku has no server-side fallback: sending it would 400
    assert ("server-side-fallback-2026-07-01" in requests[0].headers.get("anthropic-beta", "")) is fallback


# --- post-ride feedback ---------------------------------------------------

async def test_new_ride_gets_feedback_and_a_tappable_notification(db, sent, monkeypatch):
    calls = fake_llm(monkeypatch)
    monkeypatch.setattr(ha, "SUPERVISOR_TOKEN", "t")
    monkeypatch.setattr(ha, "addon_slug", lambda: "local_training_coach")
    ride(db)
    cfg = Config(anthropic_api_key="k", notify_service="notify.mobile_app_phone")

    assert await feedback.after_sync(db, cfg, NOW) == 1
    assert calls[0]["purpose"] == "ride" and calls[0]["schema"] == feedback.SCHEMA
    stored = db.activity(1)
    assert stored["feedback"]["headline"] == "Sweet spot nailed" and stored["feedback"]["model"] == "claude-haiku-5-5"
    assert stored["notified"] == 1
    n = sent[0]
    assert n["service"] == "notify.mobile_app_phone" and n["title"] == "Sweet spot nailed"
    assert n["message"] == "60 min · 30.0 km · TSS 60 · NP 220 W\nAll three reps on target. Rest tomorrow."
    assert n["data"]["url"] == "/hassio/ingress/local_training_coach/activity/1"
    assert n["data"]["push"] == {"thread-id": "rides"}

    assert await feedback.after_sync(db, cfg, NOW) == 0  # once per ride
    assert len(calls) == 1 and len(sent) == 1


async def test_old_rides_and_rides_without_power_data_yet_wait(db, sent, monkeypatch):
    calls = fake_llm(monkeypatch)
    ride(db, 1, start=NOW - timedelta(days=3))                     # a backfilled ride: never notified
    ride(db, 2, start=NOW - timedelta(hours=1), streams=False)     # streams come on the next sync
    cfg = Config(anthropic_api_key="k")
    assert await feedback.after_sync(db, cfg, NOW) == 0
    assert not calls and not sent
    # After 6 hours without streams it goes out anyway
    assert await feedback.after_sync(db, cfg, NOW + timedelta(hours=6)) == 1


async def test_without_claude_the_notification_still_goes_out(db, sent, monkeypatch):
    calls = fake_llm(monkeypatch)
    ride(db)
    await feedback.after_sync(db, Config(anthropic_api_key=""), NOW)
    assert not calls
    assert sent[0]["title"] == "Synced: Ride 1" and sent[0]["message"] == "60 min · 30.0 km · TSS 60 · NP 220 W"


async def test_a_failing_feedback_call_doesnt_block_the_notification(db, sent, monkeypatch):
    async def boom(*a, **kw):
        raise llm.LLMError("Rate limited")

    monkeypatch.setattr(llm, "run", boom)
    ride(db)
    await feedback.after_sync(db, Config(anthropic_api_key="k"), NOW)
    assert sent[0]["title"] == "Synced: Ride 1" and db.activity(1)["notified"] == 1


async def test_feedback_prompt_has_plan_segments_health_and_the_plans_coach(db, monkeypatch):
    calls = fake_llm(monkeypatch)
    day = (NOW - timedelta(hours=3)).date()
    plan_today(db, day)
    # 15 s samples: warmup 150 W, then reps at 225 W (90% of 250), the last one faded to 200 W
    watts = [150] * 40
    for rep in range(3):
        watts += [225 if rep < 2 else 200] * 40 + [140] * 20
    watts += [120] * 20
    a = ride(db, watts=watts)
    for k in range(20):
        db.set_health((day - timedelta(days=k)).isoformat(), "hrv", 60, "ms")
    cfg = Config(anthropic_api_key="k", health_hrv_entity="sensor.phone_heart_rate_variability")

    await feedback.generate(db, cfg, a)
    ctx = json.loads(calls[0]["messages"][0]["content"].strip("`json\n"))
    assert ctx["planned_session"]["title"] == "Sweet spot 3×10"
    assert [(s["target_w"], s["actual_w"]) for s in ctx["segments"]] == [(225, 225), (225, 225), (225, 200)]
    assert ctx["health"]["hrv"]["days_7d"] == 7
    assert ctx["week_so_far"]["tss_planned_week"] == 65 and ctx["alignment"] == "ok"
    assert "history_thin" in ctx and "decoupling_pct" not in ctx["ride"]  # intervals: decoupling means nothing
    assert "Two signals together" in calls[0]["system"]  # the planner's readiness rule, word for word
    assert "Carmichael" in calls[0]["system"]  # the coach who planned the session gives the feedback
    assert db.activity(1)["feedback"]["coach"] == "carmichael"


# --- visuals ----------------------------------------------------------------

def test_ride_profile_shares_scale_with_the_plan():
    a = {"spark_watts": [150] * 40 + [225] * 80, "ftp_used": 250, "elapsed_time": 1800}
    p = workouts.ride_profiles(a, {"title": "SST", "steps": STEPS})
    assert p["ridden"].count("<path") <= 6 and "#f2c94c" in p["ridden"]   # 90% FTP: sweet spot yellow, like the plan
    assert "<polygon" in p["planned"] and p["planned_title"] == "SST"
    assert workouts.ride_profiles({"spark_watts": None, "ftp_used": 250, "elapsed_time": 600}, None) is None
    assert workouts.ride_profiles(a, None)["planned"] is None


# --- status card ------------------------------------------------------------

def test_status_counts_the_week_and_the_next_event(db):
    today = date(2026, 10, 16)
    plan_today(db, today)
    db.add_note("2026-10-01", "event", "2026-11-13", "2026-11-13", "Gravel race 80 km")
    acts = [ride(db, start=datetime(2026, 10, 16, 7, 0, tzinfo=timezone.utc))]
    fitness = [{"date": (today - timedelta(days=29 - k)).isoformat(), "ctl": 40 + k * 0.2, "atl": 50, "tsb": -10}
               for k in range(30)]
    d = {"fitness": fitness, "ctl": fitness[-1]["ctl"], "tsb": -12}
    s = overview.status(db, Config(), acts, today, d)
    assert s["form"] == "Productive training"
    assert s["week"]["sessions"] == 1 and s["week"]["done"] == 1 and s["week"]["tss_planned"] == 65
    assert s["week"]["next"] is None   # today's session is ridden
    assert s["trend"]["ctl_change"] == pytest.approx(5.6)
    # CTL closes in on the 4-week average load (60 TSS / 28 days) instead of ramping on
    assert s["event"]["days"] == 28 and s["event"]["ctl_projected"] == round(60 / 28 + (45.8 - 60 / 28) * math.exp(-28 / 42))


def test_home_shows_status_latest_ride_and_its_feedback(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    with TestClient(main.app) as c:
        db = main.app.state.st.db
        today = date.today()
        a = ride(db, start=datetime.combine(today, datetime.min.time()).replace(hour=7, tzinfo=timezone.utc),
                 watts=[150] * 40 + [225] * 80)
        db.update_activity(a["id"], feedback=FB | {"coach": "nextlevel", "model": "claude-haiku-5-5", "at": "2026-10-16T09:00:00"})
        plan_today(db, today)
        html = c.get("/").text
        assert "status-card" in html and "Next session" in html and "Next event" in html
        assert 'aria-label="Sessions:' in html and 'aria-label="Fitness:' in html
        assert ">Rides<" in html and "Sweet spot nailed" in html and "All three reps on target" in html
        assert "Planned: Sweet spot 3×10" in html
        page = c.get(f"/activity/{a['id']}").text
        assert "Sweet spot nailed" in page and "Power by zone" in page


# --- segment alignment ------------------------------------------------------

def sst_watts(lead=0, fade=1.0, scale=1.0):
    """15 s samples of the 3×10 sweet spot workout at FTP 250, `lead` blocks of free riding first."""
    w = [100] * lead + [150] * 40
    for rep in range(3):
        w += [225 * scale * (fade if rep == 2 else 1)] * 40 + [137 * scale] * 20
    return w + [120] * 20


def seg(watts):
    return feedback.segments_vs_actual({"spark_watts": watts, "ftp_used": 250, "elapsed_time": len(watts) * 15}, STEPS)


def test_a_late_start_of_the_workout_is_lined_up():
    r = seg(sst_watts(lead=12))   # 3 min of riding before the workout started
    assert r["alignment"] == "shifted" and r["aligned_offset_s"] == 180
    assert [x["actual_w"] for x in r["segments"]] == [225, 225, 225]


def test_a_ride_that_doesnt_follow_the_workout_isnt_judged_rep_by_rep():
    r = seg([180 + (i % 7) for i in range(240)])
    assert r == {"segments": [], "alignment": "unclear"}


def test_a_uniform_offset_is_an_ftp_mismatch_not_missed_reps():
    r = seg(sst_watts(scale=0.95))   # Zwift's FTP is 5% lower than ours
    assert r["ftp_mismatch"] == {"zwift_ftp_estimate": 238, "offset_pct": -4.9}
    assert "ftp_mismatch" not in seg(sst_watts(fade=0.89))   # one faded rep is execution


def test_short_reps_are_left_out_rather_than_mixed_with_recovery():
    steps = [step("warmup", duration_s=600, power_low=0.5, power_high=0.75),
             step("intervals", repeat=8, on_s=30, off_s=30, on_power=1.2, off_power=0.5)]
    watts = [150] * 40 + [300, 300, 125, 125] * 8
    r = feedback.segments_vs_actual({"spark_watts": watts, "ftp_used": 250, "elapsed_time": len(watts) * 15}, steps)
    assert r["segments"] == []


async def test_a_swapped_day_is_judged_against_the_session_it_was(db, monkeypatch):
    calls = fake_llm(monkeypatch)
    day = (NOW - timedelta(hours=3)).date()
    plan_today(db, day - timedelta(days=1))  # planned yesterday, ridden today; Zwift names the ride after it
    a = ride(db, watts=sst_watts())
    db.update_activity(1, name="Zwift - Wed 15 Oct · Sweet spot 3×10")
    await feedback.generate(db, Config(anthropic_api_key="k"), db.activity(1))
    ctx = json.loads(calls[0]["messages"][0]["content"].strip("`json\n"))
    assert ctx["planned_session"]["title"] == "Sweet spot 3×10" and len(ctx["segments"]) == 3
    assert a and ctx["week_so_far"]["days"][-2]["status"] == "done"   # yesterday's session isn't "missed"


def test_upgrade_doesnt_notify_rides_from_before_it(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    DB(path)
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE activities DROP COLUMN notified")
    conn.execute("INSERT INTO activities(id, start_date, start_date_local) VALUES(1, '2026-10-16T08:00:00Z', '2026-10-16T10:00:00')")
    conn.commit()
    conn.close()
    assert DB(path).activities_to_notify("2026-10-01") == []


async def test_notification_data_only_goes_to_notify_services(monkeypatch):
    bodies = []

    async def call(method, path, body=None, timeout=20):
        bodies.append((path, body))

    monkeypatch.setattr(ha, "_call", call)
    monkeypatch.setattr(ha, "available", lambda: True)
    await ha.notify("notify.mobile_app_phone", "t", "m", {"url": "/x"})
    await ha.notify("persistent_notification.create", "t", "m", {"url": "/x"})
    assert bodies[0][1]["data"] == {"url": "/x"} and "data" not in bodies[1][1]


def test_model_markdown_is_never_trusted_as_html():
    assert "<script>" not in main.render_md("Nice <script>alert(1)</script> ride\n\n- one")
    assert "<li>one</li>" in main.render_md("- one")
