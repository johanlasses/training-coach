import pytest
from fastapi.testclient import TestClient

from app import llm, main
from app.config import load_config


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    checked = []

    async def fake_check(key):
        checked.append(key)
        return None if key == "sk-good" else "invalid API key"

    monkeypatch.setattr(llm, "check_key", fake_check)
    with TestClient(main.app) as c:
        c.checked = checked
        yield c


def test_settings_page_renders_and_hides_secrets(client):
    client.post("/settings", data={"anthropic_api_key": "sk-good", "strava_client_id": "123",
                                   "strava_client_secret": "topsecret"})
    html = client.get("/settings").text
    assert "sk-good" not in html and "topsecret" not in html
    assert "stored — leave blank to keep" in html
    assert "client_id=123" not in html and "/profile#conn-strava" in html  # the athlete's own sign-in is on Profile
    assert "client_id=123" in client.get("/profile").text  # Connect link is live once the app credentials exist


def test_blank_secret_keeps_stored_value_and_key_is_verified(client):
    r = client.post("/settings", data={"anthropic_api_key": "sk-good"})
    assert "Claude API key verified" in r.text
    client.post("/settings", data={"anthropic_api_key": ""})
    client.post("/profile", data={"weight_kg": "72.5"})
    cfg = load_config(main.app.state.st.db)
    assert cfg.anthropic_api_key == "sk-good"
    assert cfg.weight_kg == 72.5
    assert client.checked == ["sk-good"]  # unchanged key isn't re-checked


def test_bad_key_and_bad_number_are_reported(client):
    assert "the Claude API key didn&#39;t work: invalid API key" in client.post("/settings", data={"anthropic_api_key": "sk-bad"}).text
    r = client.post("/profile", data={"lthr": "abc"})
    assert "lthr: &#39;abc&#39; is not a number" in r.text or "lthr: 'abc' is not a number" in r.text


def test_availability_and_clear_key(client):
    client.post("/settings", data={"anthropic_api_key": "sk-good"})
    client.post("/profile", data={"availability": "1", "available_days": ["tue", "sat"], "max_weekend_minutes": "240"})
    client.post("/settings", data={"clear_anthropic_api_key": "1"})
    cfg = load_config(main.app.state.st.db)
    assert cfg.available_days == ["tue", "sat"]
    assert cfg.max_weekend_minutes == 240
    assert cfg.max_minutes(0) == 0 and cfg.max_minutes(5) == 240
    # A form without the availability section must not wipe the days
    client.post("/profile", data={"weight_kg": "70"})
    cfg = load_config(main.app.state.st.db)
    assert cfg.available_days == ["tue", "sat"]
    assert cfg.anthropic_api_key == ""


def test_pages_split_profile_and_settings(client):
    client.post("/settings", data={"anthropic_api_key": "sk-good", "strava_client_secret": "topsecret",
                                   "notify_service": "notify.mobile_app_x"})
    settings = client.get("/settings").text
    profile = client.get("/profile").text
    assert 'name="weight_kg"' not in settings and 'name="available_days"' not in settings
    assert 'name="notify_service"' in settings and "notify.mobile_app_x" in settings
    assert 'name="weight_kg"' in profile and 'name="available_days"' in profile
    assert "sk-good" not in profile and "topsecret" not in profile and 'name="anthropic_api_key"' not in profile


def test_profile_save_keeps_secrets_and_recomputes(client):
    client.post("/settings", data={"anthropic_api_key": "sk-good"})
    r = client.post("/profile", data={"weight_kg": "68", "ftp_override": "260"})
    assert "Profile saved." in r.text
    cfg = load_config(main.app.state.st.db)
    assert cfg.anthropic_api_key == "sk-good"
    assert cfg.weight_kg == 68 and cfg.ftp_override == 260


def test_goals_level_sessions_and_coach(client):
    r = client.post("/profile", data={"goals": "  Climb faster  ", "athlete_level": "advanced",
                                      "sessions_per_week": "4", "cycling_coach": "pogacar"})
    assert "Profile saved." in r.text
    cfg = load_config(main.app.state.st.db)
    assert (cfg.goals, cfg.athlete_level, cfg.sessions_per_week, cfg.cycling_coach) == ("Climb faster", "advanced", 4, "pogacar")
    r = client.post("/profile", data={"athlete_level": "pro", "cycling_coach": "lance"})
    assert "is not a valid choice" in r.text
    cfg = load_config(main.app.state.st.db)
    assert cfg.athlete_level == "advanced" and cfg.cycling_coach == "pogacar"
    html = client.get("/profile").text
    assert 'value="pogacar" checked' in html and "Climb faster" in html


def test_plan_page_shows_the_athletes_coach(client):
    st = main.app.state.st
    db = st.db
    ws = main.stats.week_start(main.date.today()).isoformat()
    first = db.save_plan(ws, "t1", "m", {"week_focus": "Base", "rationale": "", "block_week": 1, "days": []}, [], "pogacar")
    # The athlete's coach (Next Level) has no plan this week: the active one shows, marked as someone else's
    html = client.get("/plan").text
    assert "Base" in html and "Planned by Pogačar" in html and "Plan with Next Level" in html
    assert 'class="tabs"' not in html and "?coach=" not in html  # no coach tabs any more
    st.cfg.cycling_coach = "carmichael"
    second = db.save_plan(ws, "t2", "m", {"week_focus": "Crunch", "rationale": "", "block_week": 1, "days": []}, [], "carmichael")
    html = client.get("/plan").text  # switching coach on Profile shows that coach's plan
    assert "Crunch" in html and "Re-plan remaining days" in html and "Planned by" not in html
    assert f"/plan/{second}/activate" in html  # it isn't the active one yet (Base came first)
    assert "This week runs on Pogačar (San Millán)'s plan" in html  # and the page says so
    client.post(f"/plan/{second}/activate")
    html = client.get("/plan").text
    assert db.active_plan(ws)["id"] == second and f"/plan/{first}/activate" not in html and "This week runs on" not in html


async def test_a_plan_made_on_the_plan_page_runs_the_week(client, monkeypatch):
    st = main.app.state.st
    ws = main.stats.week_start(main.date.today())
    first = st.db.save_plan(ws.isoformat(), "t1", "m", {"week_focus": "Old", "rationale": "", "block_week": 1, "days": []}, [], "pogacar")

    async def generate(db, cfg, snapshot, week_start, today, coach_key):
        return db.save_plan(week_start.isoformat(), "t2", "m", {"week_focus": "New", "rationale": "", "block_week": 1,
                                                                 "days": []}, [], coach_key)

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(main.coach_mod, "generate_plan", generate)
    monkeypatch.setattr(st, "publish", noop)
    focus = lambda: st.db.active_plan(ws.isoformat())["payload"]["week_focus"]  # noqa: E731
    assert focus() == "Old" and first  # the first plan of a week is the active one
    await st.do_plan(ws, "carmichael", activate=True)  # what the Plan page's button runs
    assert focus() == "New"
    await st.do_plan(ws, "pogacar")  # without activate (auto-plan only plans weeks with no plan yet)
    assert focus() == "New"


def test_scheduler_never_starts_llm_jobs(client, monkeypatch):
    st = main.app.state.st
    st.cfg.anthropic_api_key = "k"
    started = []
    monkeypatch.setattr(st, "start_job", lambda name, coro: (coro.close(), started.append(name)))
    monkeypatch.setattr(main.asyncio, "sleep", lambda s: (_ for _ in ()).throw(SystemExit))

    class Sunday(main.datetime):
        @classmethod
        def now(cls, tz=None):
            return main.datetime(2026, 10, 18, 19, 0, tzinfo=tz)

    monkeypatch.setattr(main, "datetime", Sunday)
    with pytest.raises(SystemExit):
        import asyncio
        asyncio.run(st.scheduler())
    assert "plan" not in started and "analysis" not in started


def test_external_urls_default_to_the_real_services():
    """TC_STRAVA_URL / TC_ZWIFT_* only exist for the dev/ stack: unset, the app talks to the real APIs.
    A subprocess, because reloading the modules would break class identity for the other tests."""
    import os
    import subprocess
    import sys
    env = {k: v for k, v in os.environ.items() if not k.startswith("TC_")}
    code = "from app import strava, zwift; print(strava.API, strava.TOKEN_URL, zwift.API, zwift.TOKEN_URL)"
    out = subprocess.run([sys.executable, "-c", code], cwd="training_coach", env=env, capture_output=True, text=True, check=True).stdout.split()
    assert out[:3] == ["https://www.strava.com/api/v3", "https://www.strava.com/oauth/token", "https://us-or-rly101.zwift.com/api"]
    assert out[3].startswith("https://secure.zwift.com/")


def test_a_past_week_cant_be_planned_again(client, monkeypatch):
    st = main.app.state.st
    today = main.date.today()
    last = main.stats.week_start(today) - main.timedelta(days=7)
    st.db.save_plan(last.isoformat(), "t", "m", {"week_focus": "Done", "rationale": "", "block_week": 1, "days": []}, [],
                    main.coaches.get(st.cfg.cycling_coach).key)
    html = client.get(f"/plan?week={last}").text
    assert "Done" in html and "Re-plan remaining days" not in html and f'action="/plan?week={last}"' not in html
    assert "Re-plan" not in client.get(f"/plan?week={last + main.timedelta(days=7)}").text  # this week: no plan yet
    html = client.get(f"/plan?week={last - main.timedelta(days=7)}").text  # two weeks back, no plan
    assert "No plan was made for this week" in html and "plans from today" not in html and "Plan with" not in html
    jobs = []
    monkeypatch.setattr(st, "start_job", lambda name, coro: jobs.append(coro.close()) or True)
    r = client.post(f"/plan?week={last}")
    assert not jobs and "That week is over" in r.text
    client.post(f"/plan?week={main.stats.week_start(today)}")
    assert jobs  # the current week still plans
