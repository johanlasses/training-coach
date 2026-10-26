import time
import xml.etree.ElementTree as ET
from datetime import date, timedelta

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app import coaches, main, workouts, zwift
from app.db import DB
from app.zwift import API, TOKEN_URL, ZwiftClient, ZwiftError

from .test_workouts import STEPS, step

TOKENS = {"access_token": "a1", "refresh_token": "r1", "expires_in": 21600, "refresh_expires_in": 2592000}


def zfiles_listing(*files):
    """Encode a ZFilesProto the way Zwift returns /api/zfiles/list."""
    out = b""
    for fid, name in files:
        inner = zwift._varint(1 << 3) + zwift._varint(fid) + zwift._field(2, b"customworkouts") + zwift._field(3, name.encode())
        out += zwift._field(1, inner)
    return out


def connected(db, **tok):
    db.set("zwift_token", {"access_token": "a1", "refresh_token": "r1", "expires_at": time.time() + 3600,
                           "refresh_expires_at": time.time() + 86400} | tok)


@pytest.fixture
def db(tmp_path):
    return DB(tmp_path / "t.db")


def test_protobuf_roundtrip():
    files = zwift.decode_zfiles(zfiles_listing((7, "workouts.files"), (300, "tc-a.zwo")))
    assert files == [{"id": 7, "folder": "customworkouts", "filename": "workouts.files"},
                     {"id": 300, "folder": "customworkouts", "filename": "tc-a.zwo"}]
    enc = zwift.encode_zfile("customworkouts", "x.zwo", b"<xml/>")
    assert dict(zwift._fields(enc)) == {2: b"customworkouts", 3: b"x.zwo", 4: b"<xml/>"}


def test_manifest_keeps_own_workouts_and_replaces_ours():
    old = (b"<custom_file_directory><custom_file><name>My FTP test.zwo</name><time>1</time><guid>5</guid>"
           b"<checksum>9</checksum><deleted>false</deleted></custom_file>"
           b"<custom_file><name>tc-a.zwo</name><time>1</time><guid>1</guid><checksum>1</checksum>"
           b"<deleted>false</deleted></custom_file><deleted_files/></custom_file_directory>")
    root = ET.fromstring(zwift.upsert_manifest(old, "tc-a.zwo", b"abc", 1000))
    names = [n.findtext("name") for n in root.findall("custom_file")]
    assert names == ["My FTP test.zwo", "tc-a.zwo"]
    ours = root.findall("custom_file")[1]
    assert ours.findtext("time") == "1000" and ours.findtext("checksum") == str((-sum(b"abc")) % 256)
    assert list(root)[-1].tag == "deleted_files"
    assert ET.fromstring(zwift.upsert_manifest(b"", "tc-b.zwo", b"x", 1)).find("custom_file/name").text == "tc-b.zwo"
    with pytest.raises(ZwiftError):
        zwift.upsert_manifest(b"<html>oops", "tc-a.zwo", b"x", 1)


@respx.mock
async def test_connect_stores_tokens_not_password(db):
    login = respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKENS))
    respx.get(f"{API}/profiles/me").mock(return_value=httpx.Response(200, json={"id": 42, "firstName": "Johan", "lastName": "L"}))
    c = ZwiftClient(db)
    await c.connect(" me@example.com ", "hunter2")
    assert b"client_id=Zwift+Game+Client" in login.calls[0].request.content
    assert c.configured and db.get("zwift_profile")["firstName"] == "Johan"
    assert "hunter2" not in str(db.get("zwift_token")) + str(db.get("zwift_profile"))


@respx.mock
async def test_bad_login_and_expired_session(db):
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(401, json={"error": "invalid_grant"}))
    c = ZwiftClient(db)
    with pytest.raises(ZwiftError, match="email or password"):
        await c.connect("me@example.com", "wrong")
    connected(db, expires_at=0)
    with pytest.raises(ZwiftError, match="expired"):
        await c.upload_workout("tc-a.zwo", "<workout_file/>")
    assert not c.configured


@respx.mock
async def test_upload_puts_file_then_updates_manifest(db):
    connected(db)
    posts = respx.post(f"{API}/zfiles").mock(return_value=httpx.Response(200, content=b""))
    respx.get(f"{API}/zfiles/list").mock(return_value=httpx.Response(200, content=zfiles_listing((7, "workouts.files"))))
    respx.get(f"{API}/zfiles/7/download").mock(return_value=httpx.Response(200, content=(
        b"<custom_file_directory><custom_file><name>mine.zwo</name></custom_file><deleted_files/></custom_file_directory>")))
    await ZwiftClient(db).upload_workout("tc-a.zwo", "<workout_file/>")

    sent = [dict(zwift._fields(c.request.content)) for c in posts.calls]
    assert [s[3] for s in sent] == [b"tc-a.zwo", b"workouts.files"]
    assert sent[0][2] == b"customworkouts" and sent[0][4] == b"<workout_file/>"
    assert [n.findtext("name") for n in ET.fromstring(sent[1][4]).findall("custom_file")] == ["mine.zwo", "tc-a.zwo"]
    assert posts.calls[0].request.headers["Authorization"] == "Bearer a1"


def test_text_events_add_coach_cheers_without_stacking():
    iv = step("intervals", repeat=4, on_s=300, off_s=180, on_power=1.05, off_power=0.55, text="Settle in",
              cues=[{"offset_s": 480, "text": "Coach's own rep-2 cue"}, {"offset_s": 9999, "text": "out of range"}])
    c = coaches.get("carmichael")
    events = workouts.text_events(iv, c.cheers, c.last_rep)
    assert events[0] == (0, "Settle in")
    assert (480, "Coach's own rep-2 cue") in events and not any(t == 9999 for t, _ in events)
    assert (960, f"Rep 3/4: {c.cheers[1]}") in events
    assert (1440, f"Rep 4/4: {c.last_rep}") in events
    assert not any(m.startswith("Rep 2/4") for _, m in events)  # the coach already cued rep 2
    root = ET.fromstring(workouts.to_zwo("x", "y", [iv] + STEPS[2:], c.cheers, c.last_rep))
    assert len(root.find("workout")[0].findall("textevent")) == 4
    assert root.find("category").text == "Training Coach"


def test_no_pep_talk_on_openers_and_quiet_short_reps():
    c = coaches.get("carmichael")
    openers = step("intervals", repeat=3, on_s=60, off_s=120, on_power=0.8, off_power=0.5)
    assert [m for _, m in workouts.text_events(openers, c.cheers, c.last_rep)] == ["Rep 2/3", "Rep 3/3"]
    short = step("intervals", repeat=8, on_s=30, off_s=15, on_power=1.2, off_power=0.5)
    assert workouts.text_events(short, c.cheers, c.last_rep) == [(315, f"Rep 8/8: {c.last_rep}")]


@pytest.fixture
def client(tmp_path, monkeypatch):
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


def test_plan_shows_send_button_only_when_connected(client):
    week = f"/plan?week={client.day}"
    html = client.get(week).text
    # Not connected: the button leads to Zwift's card on Profile. No .zwo download any more (0.10.2)
    assert "Send to Zwift" not in html and 'href="/profile#conn-zwift">Connect Zwift' in html
    assert ".zwo" not in html and client.get(f"/plan/{client.plan_id}/{client.day}.zwo").status_code == 404
    connected(client.st.db)
    assert f'action="/plan/{client.plan_id}/{client.day}/zwift"' in client.get(week).text
    client.st.db.set("zwift_profile", {"id": 42, "firstName": "Johan", "lastName": "L"})
    profile = client.get("/profile").text  # connections moved from Settings to Profile in 0.9.0
    assert "Connect Zwift" not in profile and "Connected as <b>Johan L</b>" in profile
    assert "Connect Zwift" not in client.get("/settings").text


def test_a_past_session_can_be_sent_too(client):
    """A missed session from last week can still be ridden: Send shows on past days as well."""
    st = client.st
    past = (date.today() - timedelta(days=8)).isoformat()
    payload = st.db.plan(client.plan_id)["payload"]
    payload["days"][0]["date"] = past
    plan_id = st.db.save_plan(past, "2026-10-12T08:00:00", "m", payload, [], "carmichael")
    connected(st.db)
    assert f'action="/plan/{plan_id}/{past}/zwift"' in client.get(f"/plan?week={past}").text


def test_upload_route_uses_coach_voice_and_records_it(client, monkeypatch):
    connected(client.st.db)
    got = {}

    async def fake_upload(self, filename, zwo):
        got.update(filename=filename, zwo=zwo)

    monkeypatch.setattr(ZwiftClient, "upload_workout", fake_upload)
    r = client.post(f"/plan/{client.plan_id}/{client.day}/zwift")
    assert "is in your Zwift account" in r.text and "Send again" in r.text
    assert got["filename"] == workouts.filename(client.day, "VO2 5x3")
    assert coaches.get("carmichael").last_rep in got["zwo"]
    assert client.post(f"/plan/{client.plan_id}/2000-01-01/zwift").status_code == 404


def test_upload_failure_is_flashed(client, monkeypatch):
    connected(client.st.db)

    async def boom(self, filename, zwo):
        raise ZwiftError("Zwift POST /zfiles failed (500)")

    monkeypatch.setattr(ZwiftClient, "upload_workout", boom)
    assert "Couldn" in client.post(f"/plan/{client.plan_id}/{client.day}/zwift").text
