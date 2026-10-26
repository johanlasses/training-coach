import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import chat, checkin, llm, main, overview, stats
from app.config import Config
from app.db import DB

from .test_coach import day

pytestmark = pytest.mark.real_checkin

TODAY = date(2026, 10, 15)  # Thursday
WS = stats.week_start(TODAY)
CFG = Config(anthropic_api_key="k", cycling_coach="carmichael")


def ride(db, i, when: date, minutes=60, name="Ride"):
    db.upsert_activity_summary({"id": i, "name": name, "sport_type": "VirtualRide", "start_date": f"{when}T16:00:00Z",
                                "start_date_local": f"{when}T18:00:00", "distance": 30000, "moving_time": minutes * 60,
                                "elapsed_time": minutes * 60, "trainer": 1})
    db.update_activity(i, tss=60, np=220, ftp_used=250)


def week_plan(db):
    """Mon sweet spot (ridden), Tue rest, Wed VO2 (missed), Thu endurance (today)."""
    days = [day((WS + timedelta(days=k)).isoformat(), "rest") for k in range(7)]
    days[0] = day(days[0]["date"], "zwift", 60, "sweetspot") | {"title": "Sweet spot 3×12"}
    days[2] = day(days[2]["date"], "zwift", 60, "vo2max") | {"title": "VO2 5×4"}
    days[3] = day(days[3]["date"], "zwift", 75, "endurance") | {"title": "Endurance spin"}
    db.save_plan(WS.isoformat(), "2026-10-12T08:00:00", "m", {"week_focus": "Build", "rationale": "", "days": days},
                 [], "carmichael")


@pytest.fixture
def db(tmp_path):
    return DB(tmp_path / "t.db")


@pytest.fixture
def fake_llm(monkeypatch):
    calls = []

    async def run(api_key, system, messages, **kw):
        calls.append({"system": system, "messages": messages, **kw})
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps({"message": " Monday done. "}))],
                               model="claude-haiku-5-5")

    monkeypatch.setattr(llm, "run", run)
    return calls


def test_since_last_checkin_lists_each_day_with_its_status(db):
    week_plan(db)
    ride(db, 1, WS)  # Monday's session
    ride(db, 2, WS + timedelta(days=1), 30, "Coffee spin")  # Tuesday, a rest day: too short to be Wednesday's VO2
    db.set("checkin_day", WS.isoformat())  # last check-in Monday morning
    days = checkin.since_last(db, db.activities(), TODAY)
    assert [(d["date"], d["status"]) for d in days] == [
        (WS.isoformat(), "done"), ((WS + timedelta(days=1)).isoformat(), "extra"), ((WS + timedelta(days=2)).isoformat(), "missed")]
    assert days[0]["rides"][0]["tss"] == 60 and days[2]["planned"] == "VO2 5×4" and "excused_by" not in days[2]


def test_a_session_ridden_on_a_rest_day_is_a_swap_not_a_miss(db):
    week_plan(db)
    ride(db, 1, WS)
    ride(db, 2, WS + timedelta(days=1), 55, "Morning Ride")  # Wednesday's 60 min VO2, ridden on Tuesday
    db.set("checkin_day", WS.isoformat())
    days = {d["date"]: d for d in checkin.since_last(db, db.activities(), TODAY)}
    tue, wed = days[(WS + timedelta(days=1)).isoformat()], days[(WS + timedelta(days=2)).isoformat()]
    assert (tue["status"], tue["swapped_for"]) == ("swapped_in", "VO2 5×4")
    assert (wed["status"], wed["ridden_on"]) == ("swapped", tue["date"])


def test_a_sunday_session_ridden_on_saturday_counts(db):
    days = [day((WS + timedelta(days=k)).isoformat(), "rest") for k in range(7)]
    days[6] = day(days[6]["date"], "outdoor", 120, "endurance") | {"title": "Long endurance"}
    db.save_plan(WS.isoformat(), "t", "m", {"week_focus": "", "rationale": "", "days": days}, [], "nextlevel")
    ride(db, 1, WS + timedelta(days=5), 120, "Morning Ride")
    monday = WS + timedelta(days=7)
    db.set("checkin_day", (WS + timedelta(days=5)).isoformat())
    got = {d["date"]: d["status"] for d in checkin.since_last(db, db.activities(), monday)}
    assert got == {(WS + timedelta(days=5)).isoformat(): "swapped_in", (WS + timedelta(days=6)).isoformat(): "swapped"}


def test_a_ride_the_day_after_a_miss_is_back_on_the_bike(db):
    week_plan(db)
    ride(db, 1, WS)
    ride(db, 2, WS + timedelta(days=3), 30, "Short spin")  # Thursday (today), after Wednesday's missed VO2
    db.set("checkin_day", WS.isoformat())
    wed = {d["date"]: d for d in checkin.since_last(db, db.activities(), TODAY)}[(WS + timedelta(days=2)).isoformat()]
    assert (wed["status"], wed["back_on_bike"]) == ("missed", [TODAY.isoformat()])


def test_today_ridden_early_on_a_rest_day(db):
    week_plan(db)
    ride(db, 1, WS)
    ride(db, 2, WS + timedelta(days=1), 80, "Morning Ride")  # Tuesday rest day: 80 min
    ride(db, 3, WS + timedelta(days=2), 60, "VO2 5×4")  # Wednesday's VO2, named after it
    t = checkin.today_block(db, db.activities(), TODAY)
    assert (t["status"], t["ridden_on"]) == ("done_early", (WS + timedelta(days=1)).isoformat())


def test_ill_for_days_with_a_checkin_every_day(db):
    week_plan(db)
    ride(db, 1, WS)
    db.set("checkin_day", (TODAY - timedelta(days=1)).isoformat())  # checked in yesterday too
    ctx, _ = checkin.context(db, CFG, TODAY)
    # Only Wednesday is in this window, but Monday's done and Tuesday is rest: one miss, no question yet
    assert ctx["ask_how_they_are"] is False
    db.set("checkin_day", (TODAY - timedelta(days=1)).isoformat())
    ctx, _ = checkin.context(db, CFG, TODAY + timedelta(days=1))  # Friday: Wednesday and Thursday missed
    assert ctx["ask_how_they_are"] is True


def test_notes_excuse_missed_days_even_after_they_were_removed(db):
    week_plan(db)
    wed = (WS + timedelta(days=2)).isoformat()
    note = db.add_note("t", "injury", wed, wed, "Flu")
    db.delete_note(note, f"{TODAY}T08:00:00")  # "I'm better, drop the note"
    db.add_note("t", "event", wed, wed, "Club race")  # an event doesn't excuse anything
    db.set("checkin_day", WS.isoformat())
    days = {d["date"]: d for d in checkin.since_last(db, db.activities(), TODAY)}
    assert days[wed]["status"] == "missed" and days[wed]["excused_by"] == "Flu"


def test_since_last_without_a_plan_keeps_rides_and_looks_back_a_week_at_most(db):
    ride(db, 1, TODAY - timedelta(days=2))
    db.set("checkin_day", (TODAY - timedelta(days=30)).isoformat())
    days = checkin.since_last(db, db.activities(), TODAY)
    assert [(d["date"], d["status"]) for d in days] == [((TODAY - timedelta(days=2)).isoformat(), "unplanned")]
    assert checkin.worth_it(db, db.activities(), TODAY)
    assert not checkin.worth_it(DB(":memory:"), [], TODAY)
    ctx, _ = checkin.context(db, CFG, TODAY)
    assert ctx["today"] == {"session": None, "rides": [], "status": "not yet"}  # no plan: not "rest is the job"


async def test_generate_writes_a_checkin_into_the_chat(db, fake_llm):
    week_plan(db)
    ride(db, 1, WS)
    db.add_note("t", "injury", (WS + timedelta(days=2)).isoformat(), (WS + timedelta(days=2)).isoformat(), "Sore knee")
    db.add_chat(f"{TODAY - timedelta(days=1)}T20:00:00", "user", "carmichael", "Feeling a cold coming on")
    db.add_chat(f"{TODAY - timedelta(days=3)}T20:00:00", "user", "carmichael", "Older than the window")
    db.set("last_sync", {"at": f"{TODAY}T05:00:00+00:00"})
    msg_id = await checkin.generate(db, CFG, TODAY)
    m = db.chat_message(msg_id)
    assert (m["role"], m["coach"], m["kind"], m["content"]) == ("assistant", "carmichael", "checkin", "Monday done.")
    assert db.get("checkin_day") == TODAY.isoformat() and db.last_chat_id("checkin") == msg_id
    call = fake_llm[0]
    assert call["purpose"] == "checkin" and "Carmichael" in call["system"]
    ctx = json.loads(call["messages"][0]["content"].strip("`json\n"))
    assert ctx["first_checkin"] is True and ctx["today"]["session"]["title"] == "Endurance spin"
    assert ctx["today"]["status"] == "not yet" and ctx["last_sync"] == f"{TODAY}T05:00:00+00:00"
    assert ctx["this_week"]["missed"] == 1 and ctx["recent_messages"] == ["Feeling a cold coming on"]
    wed = next(d for d in ctx["since_last_checkin"] if d["planned"] == "VO2 5×4")
    assert wed["excused_by"] == "Sore knee"
    assert "readiness" not in ctx and ctx["ask_how_they_are"] is False  # no health sensors set up, one excused miss
    assert db.chat_message(msg_id)["ask"] is None  # the only miss was excused: nothing to adjust
    assert llm.MODELS["checkin"] == "claude-haiku-5-5" and "claude-haiku-5-5" not in llm.FALLBACK_MODELS


def test_today_already_ridden_after_two_misses(db):
    week_plan(db)
    # Monday and Wednesday missed, Tuesday rest
    ride(db, 1, TODAY, 80, "Endurance spin")
    db.set("checkin_day", WS.isoformat())
    ctx, _ = checkin.context(db, CFG, TODAY)
    assert ctx["today"]["status"] == "done" and ctx["today"]["rides"][0]["minutes"] == 80
    assert [d["status"] for d in ctx["since_last_checkin"]] == ["missed", "missed"]  # Tuesday's unridden rest is skipped
    assert ctx["ask_how_they_are"] is False  # riding today, the day after Wednesday's miss: they're back on the bike
    assert checkin.adjust_request(db, ctx, TODAY) is None and ctx["adjust_offered"] is False  # no session left after today
    p = db.active_plan(WS.isoformat())["payload"]
    p["days"][5] = day(p["days"][5]["date"], "outdoor", 120, "endurance") | {"title": "Long ride"}
    db.set_active_plan(WS.isoformat(), db.save_plan(WS.isoformat(), "t2", "m", p, [], "carmichael"))
    ctx, _ = checkin.context(db, CFG, TODAY)
    assert checkin.adjust_request(db, ctx, TODAY).startswith("I missed Sweet spot 3×12, VO2 5×4. Can you adjust")
    assert ctx["adjust_offered"] is True
    # On Monday, last week's misses don't reshuffle the new week
    assert checkin.adjust_request(db, ctx, WS + timedelta(days=7)) is None


async def test_chat_history_labels_the_checkin(db, monkeypatch):
    db.add_chat(f"{TODAY}T06:00:00", "user", "nextlevel", "Morning")
    db.add_chat(f"{TODAY}T07:00:00", "assistant", "nextlevel", "Rest today.", kind="checkin")
    db.add_chat(f"{TODAY}T09:00:00", "user", "nextlevel", "Why?")
    captured = {}

    async def run_validated(cfg, db, system, messages, schema, check, **kw):
        captured["messages"] = messages
        return {"reply": "Easy only.", "plan_changes": [], "add_notes": [], "remove_note_ids": [], "remember": [],
                "forget_memory_ids": []}, []

    monkeypatch.setattr(chat.coach, "run_validated", run_validated)
    await chat.respond(db, Config(anthropic_api_key="k"), {"today": TODAY.isoformat(), "load": {"ctl": 50},
                                                           "recent_rides": [], "athlete": {}}, TODAY)
    assert captured["messages"][1] == {"role": "assistant", "content": "[Your daily check-in]\nRest today."}
    assert [m["role"] for m in captured["messages"]] == ["user", "assistant", "user"]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    with TestClient(main.app) as c:
        yield c


def test_first_page_view_of_the_day_starts_one_checkin(client, monkeypatch):
    st = main.app.state.st
    started = []
    monkeypatch.setattr(st, "start_job", lambda name, coro: (coro.close(), started.append(name)))
    client.get("/")
    assert started == []  # no API key
    st.cfg.anthropic_api_key = "k"
    client.get("/")
    assert started == [] and st.db.get("checkin_tried") is None  # nothing to talk about yet: try again later today
    ride(st.db, 1, date.today() - timedelta(days=1))
    client.get("/")
    client.get("/plan")
    assert started == ["checkin"]  # once a day, whatever the page


def test_checkin_waits_for_a_fresh_sync(client, monkeypatch):
    st = main.app.state.st
    started = []
    monkeypatch.setattr(st, "start_job", lambda name, coro: (coro.close(), started.append(name)))
    st.cfg.anthropic_api_key, st.cfg.sync_interval_min = "k", 30
    st.strava.client_id, st.strava.client_secret = "id", "secret"
    st.db.set("strava_token", {"refresh_token": "r"})
    ride(st.db, 1, date.today() - timedelta(days=1))
    utc = datetime.now(timezone.utc)
    st.db.set("last_sync", {"at": (utc - timedelta(hours=3)).isoformat()})
    st.maybe_checkin()
    assert started == ["sync"] and st.checkin_after_sync and st.db.get("checkin_tried") is None  # stale: sync first
    st.jobs["sync"] = {"running": True}
    st.maybe_checkin()
    assert started == ["sync"]  # still syncing: wait
    st.db.set("last_sync", {"at": utc.isoformat()})
    st.maybe_checkin(synced=True)  # do_sync calls this when it's done
    assert started == ["sync", "checkin"] and st.db.get("checkin_tried") == date.today().isoformat()


def test_a_failed_sync_doesnt_hold_the_checkin_all_day(client, monkeypatch):
    st = main.app.state.st
    started = []
    monkeypatch.setattr(st, "start_job", lambda name, coro: (coro.close(), started.append(name)))
    st.cfg.anthropic_api_key = "k"
    st.strava.client_id, st.strava.client_secret = "id", "secret"
    st.db.set("strava_token", {"refresh_token": "r"})
    ride(st.db, 1, date.today() - timedelta(days=1))
    st.sync_tried = datetime.now(timezone.utc) - timedelta(minutes=2)  # a sync just failed; last_sync is still old
    st.maybe_checkin()
    assert started == ["checkin"]


def test_unread_checkin_dot_until_the_coach_page_is_opened(client):
    st = main.app.state.st
    st.db.set("checkin_tried", date.today().isoformat())
    assert "nav-dot" not in client.get("/").text
    st.db.add_chat(datetime.now().isoformat(timespec="seconds"), "assistant", "nextlevel", "Good week so far.",
                   kind="checkin")
    assert "nav-dot" in client.get("/").text
    html = client.get("/coach").text
    assert "Daily check-in" in html and "Good week so far." in html and "nav-dot" not in html
    assert "Adjust my week" not in html
    st.db.add_chat(datetime.now().isoformat(timespec="seconds"), "assistant", "nextlevel", "Tuesday got away.",
                   kind="checkin", ask="I missed VO2 5×4. Can you adjust the rest of my week")
    html = client.get("/coach").text
    assert "Adjust my week" in html and 'value="I missed VO2 5×4. Can you adjust the rest of my week"' in html
    assert "nav-dot" not in client.get("/").text
    st.jobs["checkin"] = {"running": True}
    assert client.get("/coach/status").json() == {"running": True}
    assert 'http-equiv="refresh"' not in client.get("/").text  # the check-in doesn't reload other pages
    st.jobs["checkin"] = {"running": False, "error": "Anthropic 529"}
    assert "Anthropic 529" not in client.get("/").text  # no banner on every page all day
    assert "Today's check-in failed: Anthropic 529" in client.get("/coach").text


def test_gauges():
    status = {"tsb": -12, "form": "Productive training", "form_colour": "good",
              "week": {"plan": True, "tss_done": 150, "tss_planned": 300, "tss_planned_to_date": 200, "sessions": 4,
                       "done": 2, "missed": 1, "partial": 0},
              "trend": {"ctl": 37, "ctl_change": 4.2, "ftp": 248}}
    form, week, sessions = overview.gauges(status, None)
    assert (sessions["text"], sessions["sub"], sessions["fill"] is not None) == ("2/4", "1 missed", True)
    fitness = [{"ctl": 30 + i / 10} for i in range(60)]
    fit = overview.gauges(status, None, fitness)[3]
    assert (fit["label"], fit["text"], fit["sub"], fit["colour"]) == ("Fitness", "37", "FTP 248 · ↑4", "good")
    assert len(fit["line"].split()) == 42  # the last six weeks
    flat = overview.gauges(status | {"trend": {"ctl": 37, "ctl_change": 0.4, "ftp": 248}}, None, fitness)[3]
    assert flat["sub"] == "FTP 248 · →0"  # not "↑0"
    assert overview.first_sentence("Steady Z2. Keep cadence high.") == "Steady Z2."
    assert overview.first_sentence("x " * 200).endswith("…") and len(overview.first_sentence("x " * 200)) <= 140
    assert (form["label"], form["text"], form["colour"]) == ("Form", "-12", "good") and len(form["bands"]) == 5
    assert form["marker"] and not form["fill"]
    assert (week["text"], week["sub"]) == ("50%", "TSS 150/300") and week["fill"] and week["tick"]
    assert overview.gauge(-60, -40, 30, overview.FORM_BANDS, "", "", "")["colour"] == "critical"  # clamped
    assert overview.gauge(40, -40, 30, overview.FORM_BANDS, "", "", "")["colour"] == "warning"
    # Form's colour is form_label's, also at the rounded boundaries
    assert overview.gauges(status | {"tsb": -25, "form_colour": "critical"}, None)[0]["colour"] == "critical"
    ready = {"hrv": {"change_pct": -10.0, "signal": True}, "rhr": {"change": 1, "signal": False}}
    g = overview.gauges(status | {"week": {"plan": False}}, ready)
    assert [x["label"] for x in g] == ["Form", "HRV", "Resting HR"]
    assert (g[1]["text"], g[1]["colour"]) == ("-10%", "critical")  # the signal, not the band edge
    sleep = {"sleep": {"mean_7d": 7.4, "baseline_mean": 7.0, "signal": False}}
    g = overview.gauges(status, ready | sleep)
    assert [x["label"] for x in g] == ["Form", "Week", "Sessions", "HRV", "Sleep"] and g[4]["colour"] == "good"
    # A resting HR signal is never hidden behind a fine sleep gauge
    g = overview.gauges(status, {"hrv": {"change_pct": 2, "signal": False}, "rhr": {"change": 5, "signal": True}} | sleep)
    assert [x["label"] for x in g] == ["Form", "Week", "Sessions", "Resting HR", "HRV"] and g[3]["colour"] == "critical"
    # An HRV jump under heavy load is amber, not fresh
    g = overview.gauges(status | {"tsb": -30}, {"hrv": {"change_pct": 15, "signal": False}})
    assert (g[3]["colour"], g[3]["sub"]) == ("warning", "high under load")


def test_week_tick_counts_today_only_once_ridden(db):
    week_plan(db)
    p = db.active_plan(WS.isoformat())
    for d in p["payload"]["days"]:
        d["target_tss"] = 50 if d["kind"] != "rest" else 0
    db.set_active_plan(WS.isoformat(), db.save_plan(WS.isoformat(), "2026-10-12T09:00:00", "m", p["payload"], [], "carmichael"))
    assert overview.week(db, db.activities(), TODAY)["tss_planned_to_date"] == 100  # Mon + Wed, not today
    ride(db, 1, TODAY)
    assert overview.week(db, db.activities(), TODAY)["tss_planned_to_date"] == 150


def test_readiness_uses_the_planner_thresholds():
    from app import health
    item = lambda **kw: {"days_7d": 6, "days_baseline": 20, "mean_7d": 7, "baseline_mean": 7} | kw  # noqa: E731
    r = health.readiness({"hrv": item(change_pct=-10), "rhr": item(change=3.9), "sleep": item(mean_7d=6.4)})
    assert "sleep" not in health.readiness({"sleep": item(mean_7d=5, days_baseline=10)})  # as untrusted as the planner sees it
    assert r == {"hrv": {"change_pct": -10, "signal": True}, "rhr": {"change": 3.9, "signal": False},
                 "sleep": {"mean_7d": 6.4, "baseline_mean": 7, "signal": True}}
    assert health.readiness({"hrv": item(change_pct=-30, days_7d=4)}) == {}  # too few days to trust
    from app.coach import HEALTH_RULES
    assert "-10%" in HEALTH_RULES and "+4 bpm" in HEALTH_RULES and "6.5 h" in HEALTH_RULES
