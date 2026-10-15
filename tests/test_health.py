from datetime import date, timedelta

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
