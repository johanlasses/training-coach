import json
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import chat, llm, main, stats
from app.config import Config
from app.db import DB

from .test_coach import day, sweetspot
from .test_workouts import step

CFG = Config(available_days=["mon", "tue", "wed", "thu", "fri", "sat", "sun"], max_weekday_minutes=90,
             max_weekend_minutes=180, anthropic_api_key="k")
TODAY = date(2026, 10, 14)  # Wednesday
WS = stats.week_start(TODAY)
SNAP = {"today": TODAY.isoformat(), "load": {"ctl": 50}, "recent_rides": [], "athlete": {}}


def week_plan():
    days = [day((WS + timedelta(days=i)).isoformat(), "zwift", 60, "endurance", 0,
                [step("steady", duration_s=3600, power_low=0.65, power_high=0.65)]) for i in range(7)]
    days[1] = day(days[1]["date"], "zwift", 60, "sweetspot", 0, sweetspot(60))
    return {"week_focus": "Build", "rationale": "", "block_week": 1, "days": days}


def reply(**kw):
    out = {"reply": "OK", "plan_changes": [], "add_notes": [], "remove_note_ids": [], "remember": [], "forget_memory_ids": []}
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(out | kw))])


@pytest.fixture
def db(tmp_path):
    return DB(tmp_path / "t.db")


@pytest.fixture
def fake_llm(monkeypatch):
    calls, answers = [], []

    async def run(api_key, system, messages, **kw):
        calls.append({"system": system, "messages": messages, **kw})
        return answers.pop(0)

    monkeypatch.setattr(llm, "run", run)
    return SimpleNamespace(calls=calls, answers=answers)


def ask(db, text, today=TODAY):
    db.add_chat(f"{today}T10:00:00", "user", "nextlevel", text)
    return chat.respond(db, CFG, SNAP, today)


async def test_away_days_become_a_proposal_and_a_note(db, fake_llm):
    plan_id = db.save_plan(WS.isoformat(), "t", "m", week_plan(), [], "carmichael")
    away = [day(d, "rest") for d in ("2026-10-15", "2026-10-16")]
    fake_llm.answers.append(reply(reply="Rest those days.", plan_changes=away, add_notes=[
        {"kind": "absence", "start": "2026-10-15", "end": "2026-10-16", "text": "Away for work"}]))
    msg_id = await ask(db, "I'm away Thu and Fri")
    call = fake_llm.calls[0]
    assert call["purpose"] == "chat" and call["effort"] == "medium"
    assert "Carmichael" in call["system"] and "chat with the athlete" in call["system"]
    ctx = json.loads(call["messages"][-1]["content"].split("```json\n")[1].rstrip("`\n"))
    assert ctx["open_dates"] == [(WS + timedelta(days=i)).isoformat() for i in range(2, 7)]
    assert "cues" not in ctx["active_plan"]["days"][0]["steps"][0]

    m = db.chat_message(msg_id)
    assert m["proposal"]["plan_id"] == plan_id and m["warnings"] == []
    assert m["notes"] == ["Away for work (15 Oct–16 Oct)"]
    assert db.active_notes(TODAY.isoformat())[0]["kind"] == "absence"

    new_id, text = chat.apply(db, m, TODAY)
    assert "2 days changed" in text
    active = db.active_plan(WS.isoformat())
    assert active["id"] == new_id and active["coach"] == "carmichael"
    assert [d["kind"] for d in active["payload"]["days"][3:5]] == ["rest", "rest"]
    assert active["payload"]["days"][0]["kind"] == "zwift" and active["payload"]["revised_from"] == plan_id
    assert chat.apply(db, db.chat_message(msg_id), TODAY)[0] is None  # only once


async def test_bad_changes_get_one_correction_round(db, fake_llm):
    db.save_plan(WS.isoformat(), "t", "m", week_plan(), [], "nextlevel")
    past = [day("2026-10-12", "rest")]
    too_long = [day("2026-10-15", "zwift", 200, "endurance", 0, [step("steady", duration_s=12000, power_low=0.6, power_high=0.6)])]
    fake_llm.answers += [reply(plan_changes=past), reply(plan_changes=too_long)]
    m = db.chat_message(await ask(db, "change Monday"))
    assert len(fake_llm.calls) == 2
    assert "may only use these dates" in fake_llm.calls[1]["messages"][-1]["content"]
    assert any("exceeds availability" in w for w in m["warnings"])  # kept, shown next to the proposal


async def test_no_plan_means_no_proposal(db, fake_llm):
    fake_llm.answers += [reply(plan_changes=[day("2026-10-15", "rest")]), reply(reply="Generate a plan first.")]
    m = db.chat_message(await ask(db, "make Thursday rest"))
    assert m["proposal"] is None and m["content"] == "Generate a plan first."


async def test_history_and_note_removal(db, fake_llm):
    old = db.add_note("t", "event", "2026-10-25", "2026-10-25", "Gravel race")
    db.add_note("t", "absence", "2026-10-01", "2026-10-03", "Expired trip")
    db.add_chat("t", "assistant", "nextlevel", "stray reply first")
    fake_llm.answers += [reply(), reply(remove_note_ids=[old, 999])]
    await ask(db, "hello")
    roles = [m["role"] for m in fake_llm.calls[0]["messages"]]
    assert roles[0] == "user" and roles[-1] == "user"
    ctx = fake_llm.calls[0]["messages"][-1]["content"]
    assert "Gravel race" in ctx and "Expired trip" not in ctx
    await ask(db, "the race is cancelled")
    assert db.active_notes(TODAY.isoformat()) == []
    assert len(fake_llm.calls[1]["messages"]) == 3  # hello, OK, new question


def test_stale_proposal_is_refused(db):
    plan_id = db.save_plan(WS.isoformat(), "t", "m", week_plan(), [], "nextlevel")
    msg_id = db.add_chat("t", "assistant", "nextlevel", "x", {"plan_id": plan_id, "days": [day("2026-10-15", "rest")]})
    assert chat.apply(db, db.chat_message(msg_id), TODAY + timedelta(days=2))[0] is None  # day has passed
    other = db.save_plan(WS.isoformat(), "t2", "m", week_plan(), [], "pogacar")
    db.set_active_plan(WS.isoformat(), other)
    assert "active plan changed" in chat.apply(db, db.chat_message(msg_id), TODAY)[1]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)

    async def fake_check(key):
        return None

    monkeypatch.setattr(llm, "check_key", fake_check)
    with TestClient(main.app) as c:
        yield c


def test_coach_page_flow(client, monkeypatch):
    st = main.app.state.st
    started = []
    monkeypatch.setattr(st, "start_job", lambda name, coro: (coro.close(), started.append(name)))
    ingress = {"X-Ingress-Path": "/api/hassio_ingress/abc"}
    r = client.post("/coach", data={"message": "  Can I race on Sunday?  "}, headers=ingress, follow_redirects=False)
    assert r.headers["location"] == "/api/hassio_ingress/abc/coach" and started == ["chat"]
    assert st.db.chat()[-1]["content"] == "Can I race on Sunday?"

    today = date.today()
    ws = stats.week_start(today)
    plan_id = st.db.save_plan(ws.isoformat(), "t", "m", {"week_focus": "x", "rationale": "", "block_week": 1,
                                                        "days": [day(today.isoformat(), "zwift", 60, "endurance")]}, [], "nextlevel")
    msg_id = st.db.add_chat(f"{today}T10:01:00", "assistant", "nextlevel", "Swap it.", {"plan_id": plan_id, "days": [
        day(today.isoformat(), "rest") | {"title": "Rest instead"}]}, [], ["B-race (8 Nov)"], ["Rides with a club on Saturdays"])
    note = st.db.add_note("t", "event", "2026-11-08", "2099-11-08", "B-race")
    html = client.get("/coach", headers=ingress).text
    assert "Rest instead" in html and f"/api/hassio_ingress/abc/coach/{msg_id}/apply" in html
    assert "Saved note: B-race (8 Nov)" in html and f"/api/hassio_ingress/abc/notes/{note}/delete" in html
    assert "Remembered: Rides with a club on Saturdays" in html
    assert 'href="/api/hassio_ingress/abc/coach"' in html
    assert 'id="chat-scroll"' in html and 'id="end"' in html and ">Today<" in html
    assert "fetch('/api/hassio_ingress/abc/coach/status')" not in html  # only polled while the coach is typing
    st.jobs["chat"] = {"running": True}
    html = client.get("/coach", headers=ingress).text
    assert "fetch('/api/hassio_ingress/abc/coach/status')" in html and "is typing" in html
    assert client.get("/coach/status").json() == {"running": True}
    st.jobs["chat"] = {"running": False}
    assert client.get("/coach/status").json() == {"running": False}

    client.post(f"/coach/{msg_id}/apply")
    assert st.db.active_plan(ws.isoformat())["payload"]["days"][0]["title"] == "Rest instead"
    assert "Applied to the plan" in client.get("/coach").text
    assert "B-race" in client.get("/profile").text
    assert st.snapshot()["coach_notes"][0]["text"] == "B-race"
    client.post(f"/notes/{note}/delete", data={"from": "profile"})
    client.post("/coach/clear")
    assert st.db.chat() == [] and st.db.active_notes(today.isoformat()) == []


def test_open_dates_skip_a_ridden_today():
    plan = {"payload": week_plan()}
    assert chat.open_dates(plan, TODAY)[0] == TODAY.isoformat()
    assert chat.open_dates(plan, TODAY, rode_today=True)[0] == (TODAY + timedelta(days=1)).isoformat()
