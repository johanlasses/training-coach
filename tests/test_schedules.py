import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import llm, main
from app.config import load_config
from app.db import DB


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)

    async def fake_check(key):
        return None

    monkeypatch.setattr(llm, "check_key", fake_check)
    with TestClient(main.app) as c:
        yield c


def tick(st, monkeypatch, when):
    """Run one scheduler tick at `when`; returns the names of the jobs it started."""
    started = []
    monkeypatch.setattr(st, "start_job", lambda name, coro: (coro.close(), started.append(name)))
    monkeypatch.setattr(main.asyncio, "sleep", lambda s: (_ for _ in ()).throw(SystemExit))

    class Fixed(main.datetime):
        @classmethod
        def now(cls, tz=None):
            return when.replace(tzinfo=tz) if tz else when

    monkeypatch.setattr(main, "datetime", Fixed)
    with pytest.raises(SystemExit):
        asyncio.run(st.scheduler())
    return started


def test_auto_plan_runs_once_in_its_slot(client, monkeypatch):
    st = main.app.state.st
    st.cfg.anthropic_api_key = "k"
    st.cfg.auto_plan, st.cfg.auto_plan_day, st.cfg.auto_plan_time = True, "sun", "20:00"
    sunday = main.datetime(2026, 10, 18, 19, 59)
    assert "plan" not in tick(st, monkeypatch, sunday)                      # before the slot
    assert "plan" in tick(st, monkeypatch, sunday.replace(hour=20, minute=5))
    assert "plan" not in tick(st, monkeypatch, sunday.replace(hour=20, minute=6))  # once per day
    st.db.set("auto_plan_ran", None)
    assert "plan" not in tick(st, monkeypatch, sunday.replace(hour=23, minute=30))  # window passed
    assert "plan" not in tick(st, monkeypatch, main.datetime(2026, 10, 19, 20, 5))  # wrong weekday


def test_auto_plan_skips_a_week_that_has_a_plan(client, monkeypatch):
    st = main.app.state.st
    st.cfg.anthropic_api_key = "k"
    st.cfg.auto_plan = True
    st.db.save_plan("2026-10-19", "t", "m", {"days": []}, [], "nextlevel")
    assert "plan" not in tick(st, monkeypatch, main.datetime(2026, 10, 18, 20, 1))


def test_auto_analysis_needs_a_key(client, monkeypatch):
    st = main.app.state.st
    st.cfg.auto_analysis, st.cfg.auto_analysis_day, st.cfg.auto_analysis_time = True, "mon", "07:00"
    st.cfg.anthropic_api_key = ""
    assert "analysis" not in tick(st, monkeypatch, main.datetime(2026, 10, 19, 7, 1))
    st.cfg.anthropic_api_key = "k"
    assert "analysis" in tick(st, monkeypatch, main.datetime(2026, 10, 19, 7, 2))


def test_sync_interval_and_off(client, monkeypatch):
    st = main.app.state.st
    monkeypatch.setattr(type(st.strava), "configured", property(lambda self: True))
    now = main.datetime(2026, 10, 18, 12, 0)
    st.db.set("last_sync", {"at": main.datetime(2026, 10, 18, 11, 40, tzinfo=main.timezone.utc).isoformat()})
    st.cfg.sync_interval_min = 15
    assert "sync" in tick(st, monkeypatch, now)
    st.cfg.sync_interval_min = 30
    assert "sync" not in tick(st, monkeypatch, now)
    st.cfg.sync_interval_min = 0
    st.db.set("last_sync", None)
    assert "sync" not in tick(st, monkeypatch, now)


def test_failing_sync_waits_an_interval_before_retrying(client, monkeypatch):
    # A failed sync never updates last_sync; without its own throttle it retried every minute.
    st = main.app.state.st
    monkeypatch.setattr(type(st.strava), "configured", property(lambda self: True))
    st.db.set("last_sync", None)
    st.cfg.sync_interval_min = 30
    now = main.datetime(2026, 10, 18, 12, 0)
    assert "sync" in tick(st, monkeypatch, now)
    assert "sync" not in tick(st, monkeypatch, now + main.timedelta(minutes=1))
    assert "sync" in tick(st, monkeypatch, now + main.timedelta(minutes=31))


def test_schedule_form_saves_and_unticks(client):
    client.post("/settings", data={"_section": "schedules", "auto_plan": "on", "auto_plan_day": "sat",
                                   "auto_plan_time": "18:30", "sync_interval_min": "60", "notify_time": ""})
    cfg = load_config(main.app.state.st.db)
    assert cfg.auto_plan and not cfg.zwift_refresh and not cfg.auto_analysis
    assert (cfg.auto_plan_day, cfg.auto_plan_time, cfg.sync_interval_min, cfg.notify_time) == ("sat", "18:30", 60, "")
    r = client.post("/settings", data={"_section": "schedules", "auto_plan_time": "25:99", "sync_interval_min": "7",
                                       "auto_plan_day": "funday"})
    assert "is not a time" in r.text and "not a valid choice" in r.text
    cfg = load_config(main.app.state.st.db)
    assert not cfg.auto_plan and cfg.auto_plan_time == "18:30" and cfg.sync_interval_min == 60
    # Other forms leave the bools alone
    client.post("/settings", data={"_section": "schedules", "auto_analysis": "on"})
    client.post("/profile", data={"weight_kg": "70"})
    assert load_config(main.app.state.st.db).auto_analysis


def test_settings_shows_schedules_and_usage(client):
    db = main.app.state.st.db
    msg = SimpleNamespace(model="claude-opus-5-5", usage=SimpleNamespace(input_tokens=1200, output_tokens=3400,
                                                                         cache_read_input_tokens=0))
    db.log_llm(main.datetime.now().isoformat(timespec="seconds"), "plan", msg)
    html = client.get("/settings").text
    assert 'name="auto_plan"' in html and 'name="sync_interval_min"' in html
    assert "3,400" in html and "Plan" in html


def test_log_llm_sums_per_purpose(tmp_path):
    db = DB(tmp_path / "t.db")
    for purpose, ts in [("plan", "2026-10-01T10:00:00"), ("plan", "2026-10-15T10:00:00"), ("chat", "2026-10-15T11:00:00")]:
        db.log_llm(ts, purpose, SimpleNamespace(model="m", usage=SimpleNamespace(
            input_tokens=100, output_tokens=10, cache_read_input_tokens=50)))
    assert db.llm_usage("2026-10-12")["plan"] == {"calls": 1, "input_tokens": 150, "output_tokens": 10,
                                                   "models": ["m"], "cost": 0.0}
    assert db.llm_usage()["plan"]["calls"] == 2 and db.llm_usage()["chat"]["calls"] == 1


def test_next_run():
    now = main.datetime(2026, 10, 15, 21, 0)  # Thursday
    assert main.next_run(now, "20:00") == main.datetime(2026, 10, 16, 20, 0)
    assert main.next_run(now, "20:00", "sun") == main.datetime(2026, 10, 18, 20, 0)
    assert main.next_run(now, "") is None


def test_health_read_is_hourly_and_only_when_configured(client, monkeypatch):
    st = main.app.state.st
    monkeypatch.setattr(main.ha, "available", lambda: True)
    now = main.datetime(2026, 10, 15, 9, 0)
    assert "health" not in tick(st, monkeypatch, now)  # no sensors picked
    st.cfg.health_hrv_entity = "sensor.phone_health_hrv"
    runs = [tick(st, monkeypatch, t) for t in (now, now.replace(minute=30), now.replace(hour=10))]
    assert ["health" in r for r in runs] == [True, False, True]


def test_busy_job_leaves_the_auto_plan_slot_open(client, monkeypatch):
    st = main.app.state.st
    st.cfg.anthropic_api_key, st.cfg.auto_plan = "k", True
    monkeypatch.setattr(main.asyncio, "sleep", lambda s: (_ for _ in ()).throw(SystemExit))
    attempts = []
    monkeypatch.setattr(st, "start_job", lambda name, coro: (coro.close(), attempts.append(name), False)[2])

    class Fixed(main.datetime):
        @classmethod
        def now(cls, tz=None):
            return main.datetime(2026, 10, 18, 20, 1, tzinfo=tz)

    monkeypatch.setattr(main, "datetime", Fixed)
    with pytest.raises(SystemExit):
        asyncio.run(st.scheduler())
    assert attempts == ["plan"] and st.db.get("auto_plan_ran") is None  # tried again on the next tick


def test_unscored_ride_pages_render(client):
    # A ride with power data but no FTP yet (sync died before recompute) used to 500 home and the ride page.
    from app.strava import summary_from_strava
    from tests.test_sync import strava_activity
    st = main.app.state.st
    st.db.upsert_activity_summary(summary_from_strava(strava_activity(7, start=main.date.today().isoformat() + "T08:00:00Z")))
    st.db.update_activity(7, streams_fetched=1, power_hist={200: 3600}, np=200)
    assert st.db.activity(7)["ftp_used"] is None
    assert client.get("/").status_code == 200
    assert client.get("/activity/7").status_code == 200


def test_plan_button_and_auto_plan_reach_generate_plan(client, monkeypatch):
    # 0.4.0 dropped State.do_plan, so "Generate plan" 500'd and the auto plan failed. Run the jobs for real.
    st, calls = main.app.state.st, []

    async def fake_generate(db, cfg, snap, week_start, today, coach_key=None):
        calls.append((week_start, coach_key))

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(main.coach_mod, "generate_plan", fake_generate)
    monkeypatch.setattr(st, "publish", noop)
    monkeypatch.setattr(main.ha, "notify", noop)
    jobs = []
    monkeypatch.setattr(st, "start_job", lambda name, coro: jobs.append(coro) or True)
    r = client.post("/plan?week=2026-10-19", data={"coach": "carmichael"}, follow_redirects=False)
    assert r.status_code == 303
    asyncio.run(jobs[0])
    asyncio.run(st.do_auto_plan(main.date(2026, 10, 26)))
    assert calls == [(main.date(2026, 10, 19), "carmichael"), (main.date(2026, 10, 26), st.cfg.default_coach)]


def test_usage_cost_uses_each_models_price(tmp_path):
    from app import llm
    db = DB(tmp_path / "t.db")
    for purpose, model in [("plan", "claude-opus-5-5"), ("ride", "claude-haiku-5-5")]:
        db.log_llm("2026-10-15T10:00:00", purpose, SimpleNamespace(model=model, usage=SimpleNamespace(
            input_tokens=1_000_000, output_tokens=0, cache_read_input_tokens=0)))
    usage = db.llm_usage(cost=llm.cost)
    assert usage["plan"]["cost"] == 4.0 and usage["ride"]["cost"] == 0.10
