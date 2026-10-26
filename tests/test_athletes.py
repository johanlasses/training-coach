"""Several athletes on one add-on: who's who comes from Supervisor's ingress headers, each has their own DB."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import ha, health, llm, main
from app.config import load_config

OWNER, ANNA, EVE = "a" * 32, "b" * 32, "c" * 32


def as_user(uid, name):
    return {"X-Remote-User-Id": uid, "X-Remote-User-Display-Name": name}


def st(eid, attrs):
    return {"entity_id": eid, "state": "home", "attributes": attrs}


STATES = [
    st("person.johan", {"user_id": OWNER, "device_trackers": ["device_tracker.johan_iphone"]}),
    st("person.anna", {"user_id": ANNA, "device_trackers": ["device_tracker.anna_iphone"]}),
    {"entity_id": "sensor.johan_iphone_resting_heart_rate", "state": "48", "attributes": {"unit_of_measurement": "bpm"}},
    {"entity_id": "sensor.anna_iphone_resting_heart_rate", "state": "55", "attributes": {"unit_of_measurement": "bpm"}},
]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    monkeypatch.setattr(main, "INGRESS_IP", "testclient")  # TestClient's address stands in for Supervisor's
    monkeypatch.setattr(ha, "SUPERVISOR_TOKEN", "supervisor-token")

    async def fake_check(key):
        return None

    async def fake_states():
        return STATES

    async def fake_services():
        return ["notify.mobile_app_anna_iphone", "notify.mobile_app_johan_iphone", "notify.notify"]

    async def fake_owner():
        return OWNER

    monkeypatch.setattr(llm, "check_key", fake_check)
    monkeypatch.setattr(ha, "states", fake_states)
    monkeypatch.setattr(ha, "notify_services", fake_services)
    monkeypatch.setattr(ha, "owner_user_id", fake_owner)
    with TestClient(main.app) as c:
        yield c


def let_in(client, uid, name):
    assert client.get("/", headers=as_user(uid, name)).status_code == 403
    client.post(f"/athletes/{uid}/approve", headers=as_user(OWNER, "Johan"))


def test_routes_never_reach_the_owner_state_directly():
    # app.state.st is the owner's: a route using it would show the owner's data to everyone
    src = Path(main.__file__).read_text()
    assert src.count("app.state.st") == 1 and "app.state.st = reg.owner" in src


def test_owner_is_home_assistants_owner_not_the_first_visitor(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    monkeypatch.setattr(main, "INGRESS_IP", "testclient")
    monkeypatch.setattr(ha, "SUPERVISOR_TOKEN", "supervisor-token")
    owner = None

    async def fake_owner():
        return owner

    monkeypatch.setattr(ha, "owner_user_id", fake_owner)
    with TestClient(main.app) as c:
        assert c.get("/", headers=as_user(ANNA, "Anna")).status_code == 503  # HA not answering: nobody gets in
        assert main.app.state.athletes.owner.db.get("owner_user_id") is None
        owner = OWNER
        c.portal.call(main.app.state.athletes.find_owner)
        assert c.get("/", headers=as_user(ANNA, "Anna")).status_code == 403
        assert c.get("/", headers=as_user(OWNER, "Johan")).status_code == 200


def test_headers_only_trusted_from_supervisor(client, monkeypatch):
    assert client.get("/").status_code == 403  # no user header
    monkeypatch.setattr(main, "INGRESS_IP", "172.30.32.2")
    assert client.get("/", headers=as_user(OWNER, "Johan")).status_code == 403  # another add-on spoofing it
    monkeypatch.setattr(main, "INGRESS_IP", "testclient")
    assert client.get("/", headers=as_user("../../etc", "x")).status_code == 403


def test_unknown_user_asks_and_owner_approves(client, tmp_path):
    r = client.get("/plan", headers=as_user(ANNA, "Anna"))
    assert r.status_code == 403 and "Hi Anna" in r.text
    owner = as_user(OWNER, "Johan")
    html = client.get("/settings", headers=owner).text
    assert "Asking for access" in html and "Anna" in html
    assert client.post(f"/athletes/{ANNA}/approve", headers=as_user(ANNA, "Anna")).status_code == 403  # not the owner
    client.post(f"/athletes/{ANNA}/approve", headers=owner)
    assert client.get("/", headers=as_user(ANNA, "Anna")).status_code == 200
    assert (tmp_path / "athletes" / ANNA / "training.db").exists()
    assert "Claude (AI coach and analysis)" not in client.get("/settings", headers=as_user(ANNA, "Anna")).text
    client.post(f"/athletes/{ANNA}/remove", headers=owner)
    assert client.get("/", headers=as_user(ANNA, "Anna")).status_code == 403


def test_athletes_data_and_settings_are_separate(client):
    let_in(client, ANNA, "Anna")
    reg = main.app.state.athletes
    reg.owner.db.set("strava_token", {"refresh_token": "johan"})
    client.post("/profile", headers=as_user(ANNA, "Anna"), data={"availability": "1", "available_days": ["sat"],
                                                                  "max_weekend_minutes": "240", "weight_kg": "61"})
    anna = reg.others[ANNA]
    assert anna.cfg.available_days == ["sat"] and anna.cfg.weight_kg == 61
    assert reg.owner.cfg.available_days != ["sat"] and reg.owner.cfg.weight_kg == 75
    assert anna.db.get("strava_token") is None  # Anna connects her own Strava


def test_shared_claude_key_and_strava_app_never_saved_in_athletes_db(client):
    owner = as_user(OWNER, "Johan")
    client.post("/settings", headers=owner, data={"anthropic_api_key": "sk-owner", "strava_client_id": "111",
                                                  "strava_client_secret": "owner-secret"})
    let_in(client, ANNA, "Anna")
    anna = main.app.state.athletes.others[ANNA]
    assert anna.cfg.anthropic_api_key == "sk-owner" and anna.strava.has_app
    client.post("/settings", headers=as_user(ANNA, "Anna"), data={"anthropic_api_key": "sk-anna", "_section": "schedules"})
    raw = anna.db.get("settings")  # what's on disk, before load_config's fallbacks
    assert raw["anthropic_api_key"] == "" and raw["strava_client_id"] == "" and raw["strava_client_secret"] == ""
    assert anna.cfg.anthropic_api_key == "sk-owner"  # her attempt to set a key was ignored, the owner's still applies
    html = client.get("/settings", headers=as_user(ANNA, "Anna")).text
    assert 'value="111"' not in html and "s Strava API app. Connect" in html
    # Her own Strava app replaces the shared one
    client.post("/settings", headers=as_user(ANNA, "Anna"), data={"strava_client_id": "222", "strava_client_secret": "anna-secret"})
    assert load_config(anna.db).strava_client_id == "222" and anna.cfg.strava_client_secret == "anna-secret"


def test_athlete_notify_and_health_only_from_own_phone(client):
    let_in(client, ANNA, "Anna")
    anna, h = main.app.state.athletes.others[ANNA], as_user(ANNA, "Anna")
    html = client.get("/settings", headers=h).text
    assert 'value="notify.mobile_app_anna_iphone"' in html and "johan_iphone" not in html
    client.post("/settings", headers=h, data={"_section": "schedules", "notify_service": "notify.mobile_app_johan_iphone"})
    assert anna.cfg.notify_service == ""
    client.post("/settings", headers=h, data={"_section": "schedules", "notify_service": "notify.mobile_app_anna_iphone"})
    assert anna.cfg.notify_service == "notify.mobile_app_anna_iphone"
    html = client.get("/profile", headers=h).text
    assert "sensor.anna_iphone_resting_heart_rate" in html and "sensor.johan_iphone" not in html
    client.post("/profile", headers=h, data={"health_rhr_entity": "sensor.johan_iphone_resting_heart_rate"})
    assert anna.cfg.health_rhr_entity == ""
    # Backstop: whatever ends up in the DB, an athlete never gets a non-phone service the admin token would call
    anna.db.set("settings", {"notify_service": "lock.unlock", "health_rhr_entity": "person.johan"})
    anna.reload()
    assert anna.cfg.notify_service == "" and anna.cfg.health_rhr_entity == ""


def test_new_athlete_fills_profile_before_planning(client):
    let_in(client, ANNA, "Anna")
    h = as_user(ANNA, "Anna")
    assert "Fill in your Profile" in client.get("/plan", headers=h).text
    client.post("/plan", headers=h)
    assert not main.app.state.athletes.others[ANNA].jobs.get("plan")
    client.post("/profile", headers=h, data={"goals": "Vätternrundan"})
    assert "Fill in your Profile" not in client.get("/plan", headers=h).text


def test_only_the_owner_publishes_sensors(client, monkeypatch):
    sent = []

    async def fake_publish(state):
        sent.append(state)

    monkeypatch.setattr(ha, "publish_sensors", fake_publish)
    let_in(client, ANNA, "Anna")
    reg = main.app.state.athletes
    client.portal.call(reg.others[ANNA].publish)
    assert sent == []
    client.portal.call(reg.owner.publish)
    assert len(sent) == 1


def test_athletes_survive_a_restart(tmp_path, monkeypatch, client):
    let_in(client, ANNA, "Anna")
    reg = main.Athletes()
    assert list(reg.others) == [ANNA] and reg.others[ANNA].name == "Anna"


def test_health_candidates_and_user_devices(client):
    assert client.portal.call(ha.user_devices, ANNA) == ["anna_iphone"]
    assert client.portal.call(ha.user_devices, EVE) == []
    ids = [c["entity_id"] for c in health.candidates(STATES, ["anna_iphone"])]
    assert ids == ["sensor.anna_iphone_resting_heart_rate"]


def test_athletes_see_the_owners_name_after_a_restart(client):
    client.get("/", headers=as_user(OWNER, "Johan"))
    let_in(client, ANNA, "Anna")
    reg = main.Athletes()
    assert reg.owner.name == "Johan"


def test_half_an_own_strava_app_keeps_the_shared_one(client):
    client.post("/settings", headers=as_user(OWNER, "Johan"), data={"strava_client_id": "111", "strava_client_secret": "owner-secret"})
    let_in(client, ANNA, "Anna")
    anna, h = main.app.state.athletes.others[ANNA], as_user(ANNA, "Anna")
    r = client.post("/settings", headers=h, data={"strava_client_id": "222", "strava_client_secret": ""})
    assert "enter both" in r.text
    raw = anna.db.get("settings")
    assert raw["strava_client_secret"] == "" and raw["strava_client_id"] == ""
    assert anna.cfg.strava_client_id == "111"  # still the shared app


def test_a_phone_name_prefix_does_not_reach_another_phone():
    assert health.phone_sensors(["iphone"]) & {"sensor.iphone_2_resting_heart_rate"} == set()
    states = [{"entity_id": "sensor.iphone_2_resting_heart_rate", "state": "50", "attributes": {"unit_of_measurement": "bpm"}},
              {"entity_id": "sensor.iphone_resting_heart_rate", "state": "60", "attributes": {"unit_of_measurement": "bpm"}}]
    assert [c["entity_id"] for c in health.candidates(states, ["iphone"])] == ["sensor.iphone_resting_heart_rate"]
    assert not main.own_phone("health_rhr_entity", "sensor.iphone_2_resting_heart_rate", ["iphone"])
