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
    assert db.llm_usage("2026-10-12")["plan"] == {"purpose": "plan", "calls": 1, "input_tokens": 150, "output_tokens": 10}
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
