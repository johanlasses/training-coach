import json
import sqlite3
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import analysis, chat, coach, coaches, feedback, llm, main, memory, stats
from app.config import Config
from app.db import DB

from .test_chat import CFG, SNAP, TODAY, WS, reply, week_plan
from .test_coach import day
from .test_feedback import plan_today, ride

NOON = datetime(2026, 10, 14, 12)
INGRESS = {"X-Ingress-Path": "/api/hassio_ingress/abc"}


@pytest.fixture
def db(tmp_path):
    return DB(tmp_path / "t.db")


@pytest.fixture
def calls(monkeypatch):
    out, answers = [], []

    async def run(api_key, system, messages, **kw):
        out.append({"system": system, "messages": messages, **kw})
        return answers.pop(0)

    monkeypatch.setattr(llm, "run", run)
    return SimpleNamespace(calls=out, answers=answers)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)

    async def fake_check(key):
        return None

    monkeypatch.setattr(llm, "check_key", fake_check)
    with TestClient(main.app) as c:
        yield c


def context_of(call, i=-1):
    return json.loads(call["messages"][i]["content"].split("```json\n")[1].rstrip("`\n"))


# --- the change log ---------------------------------------------------------

def test_profile_changes_are_logged_once(client):
    st = main.app.state.st
    client.post("/profile", data={"goals": "Gravel 160 km in June", "cycling_coach": "carmichael", "weight_kg": "76.5",
                                  "ftp_override": "280", "availability": "1", "available_days": ["mon", "wed", "sat"]})
    texts = [e["text"] for e in st.db.log_entries()]
    assert "Goals: none → “Gravel 160 km in June”" in texts
    assert "Cycling coach: Next Level → Carmichael (Time-Crunched)" in texts
    assert "Weight: 75 kg → 76.5 kg" in texts and "FTP override: auto → 280 W" in texts
    assert "Training days: Mon, Tue, Wed, Thu, Sat, Sun → Mon, Wed, Sat" in texts
    assert st.db.last_log("coach", "9999")["data"] == {"from": "nextlevel", "to": "carmichael", "sport": "cycling"}
    client.post("/profile", data={"goals": "Gravel 160 km in June", "weight_kg": "76.5"})  # nothing changed
    client.post("/settings", data={"notify_service": "notify.other"})  # not athlete data
    assert len(st.db.log_entries()) == len(texts)


def test_long_goals_are_cut_in_the_log():
    assert memory._show("goals", "x" * 400) == f"“{'x' * 300}…”"


# --- who is in charge -------------------------------------------------------

def test_coach_in_charge_follows_the_latest_decision(db):
    db.save_plan(WS.isoformat(), "2026-10-12T08:00:00", "m", week_plan(), [], "carmichael")
    assert memory.coach_in_charge(db, CFG, NOON).key == "carmichael"
    db.log("2026-10-13T09:00:00", "coach", "Coach: …", {"from": "carmichael", "to": "pogacar"})
    assert memory.coach_in_charge(db, CFG, NOON).key == "pogacar"
    assert memory.coach_in_charge(db, CFG, datetime(2026, 10, 12, 20)).key == "carmichael"  # before the switch
    assert memory.coach_in_charge(db, CFG, datetime(2026, 10, 21, 12)).key == "pogacar"  # next week, no plan yet
    newer = db.save_plan(WS.isoformat(), "2026-10-14T08:00:00", "m", week_plan(), [], "carmichael")
    db.set_active_plan(WS.isoformat(), newer)
    assert memory.coach_in_charge(db, CFG, NOON).key == "carmichael"  # a plan made after the switch wins


def test_coach_timeline(db):
    for n, key in ((3, "carmichael"), (2, "carmichael"), (1, "nextlevel")):
        ws = WS - timedelta(weeks=n)
        db.save_plan(ws.isoformat(), f"{ws - timedelta(days=1)}T20:00:00", "m", week_plan(), [], key)
    runs = memory.coach_timeline(db, CFG, NOON)
    assert [(r["coach"], r["from"], r["to"], r["weeks_planned"]) for r in runs] == [
        ("Carmichael (Time-Crunched)", "2026-09-21", "2026-10-04", 2), ("Next Level", "2026-10-05", None, 1)]
    assert runs[0]["approach"] == coaches.COACHES["carmichael"].summary

    db.save_plan(WS.isoformat(), "2026-10-11T20:00:00", "m", week_plan(), [], "nextlevel")
    db.log("2026-10-13T09:00:00", "coach", "Coach: …", {"from": "nextlevel", "to": "pogacar"})
    runs = memory.coach_timeline(db, CFG, NOON)
    assert [(r["coach"], r["from"], r["to"], r["weeks_planned"]) for r in runs[1:]] == [
        ("Next Level", "2026-10-05", "2026-10-12", 2), ("Pogačar (San Millán)", "2026-10-13", None, 0)]


def test_a_switch_before_the_plan_week_drops_that_plan(db):
    db.save_plan(WS.isoformat(), "2026-10-10T20:00:00", "m", week_plan(), [], "nextlevel")
    db.log("2026-10-11T09:00:00", "coach", "Coach: …", {"from": "nextlevel", "to": "pogacar"})
    assert [(r["coach"], r["from"], r["to"]) for r in memory.coach_timeline(db, CFG, NOON)] == [
        ("Pogačar (San Millán)", "2026-10-11", None)]


# --- notes and facts ----------------------------------------------------------

def test_removed_and_expired_notes_stay_in_the_history(db):
    gone = db.add_note("t", "injury", "2026-10-10", "2026-10-30", "Sore knee")
    db.add_note("t", "event", "2026-09-06", "2026-09-06", "Gravel race 80 km")
    db.add_note("t", "absence", "2026-10-20", "2026-10-22", "Work trip")
    db.add_note("t", "event", "2025-06-01", "2025-06-01", "Too long ago")
    db.delete_note(gone, "2026-10-13T08:00:00")
    assert [n["text"] for n in db.active_notes("2026-10-14")] == ["Work trip"]
    trip = db.add_note("t", "absence", "2026-10-24", "2026-10-25", "Trip that fell through")
    db.delete_note(trip, "2026-10-14T08:00:00")
    assert memory.history(db, CFG, NOON)["past_notes"] == [
        {"kind": "injury", "start": "2026-10-10", "end": "2026-10-13", "text": "Sore knee", "removed": "2026-10-13"},
        {"kind": "absence", "start": "2026-10-24", "end": "2026-10-25", "text": "Trip that fell through", "cancelled": True},
        {"kind": "event", "start": "2026-09-06", "end": "2026-09-06", "text": "Gravel race 80 km"}]


def test_migration_adds_the_memory_columns(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE coach_notes (id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT, kind TEXT, start TEXT, end TEXT, text TEXT);
        CREATE TABLE chat_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, role TEXT, coach TEXT, content TEXT,
            proposal TEXT, warnings TEXT, notes TEXT, applied_plan_id INTEGER);
        INSERT INTO coach_notes(created, kind, start, end, text) VALUES('t', 'event', '2099-01-01', '2099-01-01', 'Race');
        INSERT INTO chat_messages(ts, role, coach, content) VALUES('2026-10-01T10:00:00', 'user', 'nextlevel', 'hi');""")
    conn.commit()
    conn.close()
    db = DB(path)
    assert db.active_notes("2026-10-14")[0]["text"] == "Race"
    assert db.chat()[0]["remembered"] == []


async def test_chat_remembers_forgets_and_knows_the_earlier_coach(db, calls):
    old = db.add_memory("2026-09-01T10:00:00", "carmichael", "Prefers mornings")
    db.add_chat("2026-10-13T10:00:00", "user", "carmichael", "hi")
    db.add_chat("2026-10-13T10:01:00", "assistant", "carmichael", "Hello from Carmichael")
    db.add_chat(f"{TODAY}T10:00:00", "user", "nextlevel", "I work nights now")
    calls.answers.append(reply(remember=["Works night shifts", " Works night shifts ", ""], forget_memory_ids=[old, 999]))
    snap = SNAP | {"athlete_history": {"coaches": [], "memory": ["Prefers mornings"]}}
    msg_id = await chat.respond(db, CFG, snap, TODAY)

    msgs = calls.calls[0]["messages"]
    assert msgs[1]["content"] == "[Earlier coach: Carmichael (Time-Crunched)]\nHello from Carmichael"
    ctx = context_of(calls.calls[0])
    assert ctx["memory"] == [{"id": old, "since": "2026-09-01", "text": "Prefers mornings"}]
    assert ctx["snapshot"]["athlete_history"] == {"coaches": []}  # memory is sent once, with ids
    assert [(f["text"], f["coach"]) for f in db.memories()] == [("Works night shifts", "nextlevel")]
    assert db.chat_message(msg_id)["remembered"] == ["Works night shifts"]


def test_switching_coach_hands_over_the_chat(client):
    st = main.app.state.st
    today = date.today()
    st.db.save_plan(stats.week_start(today).isoformat(), "2000-01-01T00:00:00", "m", {
        "week_focus": "x", "rationale": "", "block_week": 1, "days": [day(today.isoformat(), "zwift", 60, "endurance")]},
        [], "nextlevel")
    assert "took over" not in client.get("/coach").text
    client.post("/profile", data={"cycling_coach": "pogacar"})
    html = client.get("/coach").text
    assert "Pogačar (San Millán) · took over this week" in html and "the plan is by Next Level" in html


# --- who reads it -------------------------------------------------------------

def test_snapshot_carries_the_history(client):
    st = main.app.state.st
    st.db.add_memory("2026-10-01T10:00:00", "nextlevel", "Rides with a club on Saturdays")
    assert st.snapshot()["athlete_history"]["memory"] == ["Rides with a club on Saturdays"]
    assert "snapshot.athlete_history" in coach.system_prompt(coaches.get("nextlevel"), coaches.get("nextlevel").rules())


async def test_analysis_sees_the_previous_one(db, calls):
    db.save_analysis("2026-10-05T07:00:00", "scheduled", "m", "## Current form\nTired.", "carmichael")
    db.save_analysis(datetime.now().isoformat(timespec="seconds"), "manual", "m", "Minutes ago", "pogacar")
    calls.answers.append(SimpleNamespace(content=[SimpleNamespace(type="text", text="## Current form\nBetter.")]))
    await analysis.run_analysis(db, CFG, SNAP, "manual")
    prev = context_of(calls.calls[0], 0)["previous_analysis"]  # not the comparison run from minutes ago
    assert prev | {"days_since": 0} == {
        "date": "2026-10-05", "days_since": 0, "coach": "Carmichael (Time-Crunched)", "markdown": "## Current form\nTired."}
    assert "previous_analysis" in calls.calls[0]["system"]


def test_ride_feedback_gets_the_new_coach_and_the_history(db):
    start = datetime(2026, 10, 14, 17, 0)
    plan_today(db, start.date())  # by Carmichael
    db.log("2026-10-13T09:00:00", "coach", "Coach: …", {"from": "carmichael", "to": "pogacar"})
    db.add_memory("2026-10-01T10:00:00", "carmichael", "Knee niggles on low-cadence work")
    ctx, c = feedback.context(db, Config(), ride(db, start=start))
    assert c.key == "pogacar"
    assert ctx["athlete_history"]["memory"] == ["Knee niggles on low-cadence work"]
    assert [r["coach"] for r in ctx["athlete_history"]["coaches"]] == ["Carmichael (Time-Crunched)", "Pogačar (San Millán)"]


def test_profile_shows_memory_and_history(client):
    st = main.app.state.st
    fact = st.db.add_memory("2026-10-01T10:00:00", "nextlevel", "Hates the turbo in summer")
    client.post("/profile", data={"goals": "Ride Vätternrundan"})
    html = client.get("/profile", headers=INGRESS).text
    assert "Hates the turbo in summer" in html and f"/api/hassio_ingress/abc/memory/{fact}/delete" in html
    assert "Goals: none → “Ride Vätternrundan”" in html
    r = client.post(f"/memory/{fact}/delete", headers=INGRESS, follow_redirects=False)
    assert r.headers["location"] == "/api/hassio_ingress/abc/profile" and st.db.memories() == []
    assert "Hates the turbo" not in client.get("/profile").text


def test_applying_the_new_coachs_proposal_keeps_the_new_coach(db):
    base = db.save_plan(WS.isoformat(), "2026-10-12T08:00:00", "m", week_plan(), [], "carmichael")
    db.log("2026-10-13T09:00:00", "coach", "Coach: …", {"from": "carmichael", "to": "pogacar"})
    msg = db.add_chat("2026-10-14T10:00:00", "assistant", "pogacar", "Easier Thursday.",
                      {"plan_id": base, "days": [day("2026-10-15", "rest")]})
    assert chat.apply(db, db.chat_message(msg), TODAY)[0]
    assert memory.coach_in_charge(db, CFG, NOON).key == "pogacar"
    assert [r["coach"] for r in memory.coach_timeline(db, CFG, NOON)] == ["Carmichael (Time-Crunched)", "Pogačar (San Millán)"]


def test_the_requesting_coach_is_marked(db):
    db.save_plan(WS.isoformat(), "2026-10-12T08:00:00", "m", week_plan(), [], "nextlevel")
    snap = {"athlete_history": memory.history(db, CFG, NOON)}
    assert [r["you"] for r in memory.for_coach(snap, coaches.get("pogacar"))["athlete_history"]["coaches"]] == [False]
    assert [r["you"] for r in memory.for_coach(snap, coaches.get("nextlevel"))["athlete_history"]["coaches"]] == [True]
    assert "you" not in snap["athlete_history"]["coaches"][0]  # the shared snapshot stays untouched


def test_a_hard_run_of_rides_already_done_is_not_an_error():
    rides = [{"date": "2026-10-12", "if": 0.9}, {"date": "2026-10-13", "if": 0.9}]  # Carmichael allowed this
    easy = [day(d, "rest") for d in ("2026-10-14", "2026-10-15")]
    rules = {"max_consecutive_hard": 1, "max_hard_sessions": 3}
    assert not [e for e in coach.hard_errors(easy, rides, rules, False) if "in a row" in e]
    hard = [day("2026-10-14", "zwift", 60, "threshold", 0, [])] + easy[1:]
    assert [e for e in coach.hard_errors(hard, rides, rules, False) if "in a row" in e] == [
        "3 hard days in a row ending 2026-10-14; at most 1"]
