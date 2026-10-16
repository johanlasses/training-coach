from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app import ha, health, llm, main
from app.config import Config, load_config
from app.db import DB

HA = "http://ha.test"
TODAY = date(2026, 10, 15)


@pytest.fixture
def ha_up(monkeypatch):
    monkeypatch.setattr(ha, "HA_URL", HA)
    monkeypatch.setattr(ha, "HA_TOKEN", "tok")


def st(entity, state, unit, name="", changed="2026-10-15T05:12:00+00:00"):
    return {"entity_id": entity, "state": state, "last_changed": changed,
            "attributes": {"unit_of_measurement": unit, "friendly_name": name}}


@respx.mock
async def test_fetch_stores_normalised_values(tmp_path, ha_up):
    db = DB(tmp_path / "t.db")
    cfg = Config(health_rhr_entity="sensor.p_health_resting_heart_rate", health_sleep_entity="sensor.p_health_sleep",
                 health_weight_entity="sensor.p_health_weight", health_hrv_entity="sensor.p_health_hrv")
    respx.get(f"{HA}/api/states/sensor.p_health_resting_heart_rate").respond(json=st("x", "52", "bpm"))
    respx.get(f"{HA}/api/states/sensor.p_health_sleep").respond(json=st("x", "435", "min"))
    respx.get(f"{HA}/api/states/sensor.p_health_weight").respond(json=st("x", "165", "lb"))
    route = respx.get(f"{HA}/api/states/sensor.p_health_hrv").respond(json=st("x", "unavailable", "ms"))
    assert await health.fetch(db, cfg, TODAY) == 3
    vals = {r["metric"]: r["value"] for r in db.health(TODAY.isoformat())}
    assert vals == {"rhr": 52, "sleep": 7.25, "weight": 74.84}
    assert route.calls.last.request.headers["authorization"] == "Bearer tok"
    respx.get(f"{HA}/api/states/sensor.p_health_resting_heart_rate").respond(json=st("x", "50", "bpm"))
    respx.get(f"{HA}/api/states/sensor.p_health_hrv").mock(side_effect=httpx.ConnectError("down"))
    await health.fetch(db, cfg, TODAY)
    assert {r["metric"]: r["value"] for r in db.health(TODAY.isoformat())}["rhr"] == 50  # last read of the day wins


@respx.mock
async def test_stale_states_are_skipped_and_morning_hrv_is_kept(tmp_path, ha_up):
    db = DB(tmp_path / "t.db")
    cfg = Config(health_rhr_entity="sensor.rhr", health_hrv_entity="sensor.hrv")
    respx.get(f"{HA}/api/states/sensor.rhr").respond(json=st("x", "55", "bpm", changed="2026-10-14T06:00:00+00:00"))
    respx.get(f"{HA}/api/states/sensor.hrv").respond(json=st("x", "62", "ms"))
    assert await health.fetch(db, cfg, TODAY) == 1  # resting HR is yesterday's number
    respx.get(f"{HA}/api/states/sensor.hrv").respond(json=st("x", "41", "ms", changed="2026-10-15T15:30:00+00:00"))
    await health.fetch(db, cfg, TODAY)
    assert [(r["metric"], r["value"]) for r in db.health(TODAY.isoformat())] == [("hrv", 62)]
    # The same resting HR as yesterday, reported again this morning, still counts for today
    same = st("x", "55", "bpm", changed="2026-10-14T06:00:00+00:00") | {"last_reported": "2026-10-15T06:00:00+00:00"}
    respx.get(f"{HA}/api/states/sensor.rhr").respond(json=same)
    await health.fetch(db, cfg, TODAY)
    assert ("rhr", 55) in [(r["metric"], r["value"]) for r in db.health(TODAY.isoformat())]


async def test_fetch_without_ha_does_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(ha, "HA_URL", "")
    assert await health.fetch(DB(tmp_path / "t.db"), Config(health_rhr_entity="sensor.x"), TODAY) == 0


def test_summary_compares_the_week_with_the_weeks_before(tmp_path):
    db = DB(tmp_path / "t.db")
    for i in range(35):
        day = (TODAY - timedelta(days=i)).isoformat()
        db.set_health(day, "hrv", 54 if i < 7 else 60, "ms")
        db.set_health(day, "rhr", 55 if i < 7 else 50, "bpm")
    db.set_health((TODAY - timedelta(days=40)).isoformat(), "sleep", 7, "h")  # too old
    s = health.summary(db, TODAY)
    assert set(s) == {"hrv", "rhr"}
    assert s["hrv"]["change_pct"] == -10.0 and s["hrv"]["baseline_mean"] == 60  # the drop isn't diluted
    assert s["rhr"]["change"] == 5 and s["rhr"]["days_7d"] == 7 and s["rhr"]["days_baseline"] == 28


def test_candidates_pick_health_sensors():
    out = health.candidates([st("sensor.phone_health_hrv", "48", "ms", "Phone HRV"), st("sensor.kitchen_temp", "21", "°C"),
                             st("binary_sensor.health_x", "on", ""), st("sensor.hae_rhr", "51", "bpm", "Health RHR")])
    assert [c["entity_id"] for c in out] == ["sensor.hae_rhr", "sensor.phone_health_hrv"]
    assert out[1]["label"] == "Phone HRV: 48 ms"


# Names and units as the HA iOS app 2026.9 reports them (no "health" in them)
IOS = [st("sensor.johan_iphone_resting_heart_rate", "52", "bpm", "Johan iPhone  Resting Heart Rate"),
       st("sensor.johan_iphone_heart_rate_variability", "103", "ms", "Johan iPhone  Heart Rate Variability"),
       st("sensor.johan_iphone_sleep_duration", "497", "min", "Johan iPhone  Sleep Duration"),
       st("sensor.johan_iphone_weight", "75.8", "kg", "Johan iPhone  Weight"),
       st("sensor.johan_iphone_vo2_max", "unavailable", "mL/kg·min", "Johan iPhone  VO2 Max"),
       st("sensor.johan_iphone_heart_rate", "72", "bpm"), st("sensor.johan_iphone_lean_body_mass", "68.9", "kg"),
       st("sensor.johan_iphone_deep_sleep", "84", "min"), st("sensor.johan_iphone_battery_level", "90", "%")]


def test_detects_ios_app_sensors():
    found = health.detect(health.candidates(IOS))
    assert found == {"rhr": "sensor.johan_iphone_resting_heart_rate", "hrv": "sensor.johan_iphone_heart_rate_variability",
                     "sleep": "sensor.johan_iphone_sleep_duration", "weight": "sensor.johan_iphone_weight"}
    # VO2 max has no value yet: listed to pick, not pre-filled
    assert "sensor.johan_iphone_vo2_max" in [c["entity_id"] for c in health.candidates(IOS)]
    # A printer's filament weight (wrong unit) or a scale with one metric never beats the phone
    others = IOS + [st("sensor.a_old_ipad_resting_heart_rate", "unavailable", "bpm"),
                    st("sensor.a_printer_filament_weight", "830", "g"), st("sensor.a_scale_weight", "76", "kg")]
    found = health.detect(health.candidates(others))
    assert found["rhr"] == "sensor.johan_iphone_resting_heart_rate" and found["weight"] == "sensor.johan_iphone_weight"
    assert health.normalise("sleep", 497, "min") == pytest.approx(8.28, abs=0.01)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)

    async def fake_check(key):
        return None

    monkeypatch.setattr(llm, "check_key", fake_check)
    with TestClient(main.app) as c:
        yield c


def test_profile_health_card_and_snapshot(client, monkeypatch):
    async def fake_states():
        return [st("sensor.phone_health_resting_heart_rate", "52", "bpm", "Phone resting HR")]

    monkeypatch.setattr(ha, "states", fake_states)
    s = main.app.state.st
    assert "health" not in s.snapshot()  # nothing configured, nothing sent to the coach
    html = client.get("/profile").text
    assert 'value="sensor.phone_health_resting_heart_rate"' in html and 'name="health_hrv_entity"' in html
    client.post("/profile", data={"health_rhr_entity": " sensor.phone_health_resting_heart_rate "})
    assert load_config(s.db).health_rhr_entity == "sensor.phone_health_resting_heart_rate"
    s.db.set_health(date.today().isoformat(), "rhr", 52, "bpm")
    s.db.set_health((date.today() - timedelta(days=10)).isoformat(), "rhr", 50, "bpm")
    assert s.snapshot()["health"]["rhr"]["latest"] == 52
    assert "Latest 52.0 bpm" in client.get("/profile").text or "Latest 52 bpm" in client.get("/profile").text


def test_profile_prefills_detected_sensors_on_first_setup_only(client, monkeypatch):
    async def fake_states():
        return IOS

    monkeypatch.setattr(ha, "states", fake_states)
    html = client.get("/profile").text
    assert 'name="health_hrv_entity" value="sensor.johan_iphone_heart_rate_variability"' in html
    assert html.count("Found in Home Assistant") == 4
    client.post("/profile", data={"health_rhr_entity": "sensor.johan_iphone_resting_heart_rate", "health_hrv_entity": ""})
    html = client.get("/profile").text  # configured now: a cleared field stays cleared
    assert 'name="health_hrv_entity" value=""' in html and "Found in Home Assistant" not in html


def test_run_sh_keeps_the_supervisor_token():
    # s6-overlay's /init in the HA base image clears the environment; without with-contenv the add-on never saw
    # SUPERVISOR_TOKEN, so HA reads, notifications and MQTT sensors silently did nothing (0.1.0-0.4.2).
    run_sh = (Path(__file__).parent.parent / "training_coach" / "run.sh").read_text()
    assert run_sh.startswith("#!/usr/bin/with-contenv ")


async def test_mqtt_discovery_names_sensors_training_star(monkeypatch):
    # HA 2026.4 ignores object_id; default_entity_id is what makes the entities sensor.training_*.
    import json

    import aiomqtt
    sent = {}

    class FakeClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def publish(self, topic, payload, retain=False):
            sent[topic] = json.loads(payload)

    async def settings():
        return {"host": "core-mosquitto", "port": 1883}

    monkeypatch.setattr(ha, "mqtt_settings", settings)
    monkeypatch.setattr(aiomqtt, "Client", FakeClient)
    await ha.publish_sensors({"ftp": 250})
    cfg = sent["homeassistant/sensor/training_coach/ftp/config"]
    assert cfg["default_entity_id"] == "sensor.training_ftp" and "object_id" not in cfg
    assert sent[ha.STATE_TOPIC] == {"ftp": 250}
