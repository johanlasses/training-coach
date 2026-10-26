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
    yesterday = "2026-10-14T06:00:00+00:00"
    respx.get(f"{HA}/api/states/sensor.rhr").respond(json=st("x", "55", "bpm", changed=yesterday))
    respx.get(f"{HA}/api/states/sensor.hrv").respond(json=st("x", "62", "ms", changed=yesterday))
    assert await health.fetch(db, cfg, TODAY) == 0  # nothing from the phone today: both are yesterday's numbers
    respx.get(f"{HA}/api/states/sensor.hrv").respond(json=st("x", "62", "ms"))
    respx.get(f"{HA}/api/states/sensor.rhr").respond(json=st("x", "56", "bpm"))
    await health.fetch(db, cfg, TODAY)
    respx.get(f"{HA}/api/states/sensor.hrv").respond(json=st("x", "41", "ms", changed="2026-10-15T15:30:00+00:00"))
    await health.fetch(db, cfg, TODAY)
    assert [(r["metric"], r["value"]) for r in db.health(TODAY.isoformat())] == [("hrv", 62), ("rhr", 56)]  # morning HRV
    # The same resting HR as yesterday, reported again this morning, still counts for today
    same = st("x", "55", "bpm", changed="2026-10-14T06:00:00+00:00") | {"last_reported": "2026-10-15T06:00:00+00:00"}
    respx.get(f"{HA}/api/states/sensor.rhr").respond(json=same)
    await health.fetch(db, cfg, TODAY)
    assert ("rhr", 55) in [(r["metric"], r["value"]) for r in db.health(TODAY.isoformat())]


@respx.mock
async def test_an_unchanged_resting_hr_counts_when_the_phone_reported_today(tmp_path, ha_up):
    """HA doesn't bump last_reported for an unchanged value: a resting HR equal to yesterday's mustn't lose the day."""
    db = DB(tmp_path / "t.db")
    cfg = Config(health_rhr_entity="sensor.johan_iphone_resting_heart_rate",
                 health_hrv_entity="sensor.johan_iphone_heart_rate_variability",
                 health_weight_entity="sensor.johan_iphone_weight")
    yesterday, old = "2026-10-14T06:00:00+00:00", "2026-10-10T06:00:00+00:00"
    respx.get(f"{HA}/api/states/sensor.johan_iphone_resting_heart_rate").respond(json=st("x", "52", "bpm", changed=yesterday))
    respx.get(f"{HA}/api/states/sensor.johan_iphone_heart_rate_variability").respond(json=st("x", "61.4", "ms", changed=yesterday))
    respx.get(f"{HA}/api/states/sensor.johan_iphone_weight").respond(json=st("x", "75", "kg", changed=old))
    battery = respx.get(f"{HA}/api/states/sensor.johan_iphone_battery_level").respond(json=st("x", "80", "%", changed=yesterday))
    assert await health.fetch(db, cfg, TODAY) == 0  # the phone sent nothing today, not even its battery level
    battery.respond(json=st("x", "78", "%"))  # it's syncing today
    assert await health.fetch(db, cfg, TODAY) == 1
    vals = {r["metric"]: r["value"] for r in db.health(TODAY.isoformat())}
    # resting HR carried; HRV never (it keeps the first reading, and the real one may still come); weight too old
    assert vals == {"rhr": 52}
    respx.get(f"{HA}/api/states/sensor.johan_iphone_resting_heart_rate").respond(json=st("x", "51", "bpm"))
    await health.fetch(db, cfg, TODAY)
    assert {r["metric"]: r["value"] for r in db.health(TODAY.isoformat())}["rhr"] == 51  # today's real value wins


@respx.mock
async def test_carried_values_are_logged(tmp_path, ha_up, caplog):
    db = DB(tmp_path / "t.db")
    cfg = Config(health_rhr_entity="sensor.p_resting_heart_rate", health_sleep_entity="sensor.p_sleep_duration")
    respx.get(f"{HA}/api/states/sensor.p_resting_heart_rate").respond(json=st("x", "52", "bpm", changed="2026-10-14T06:00:00+00:00"))
    respx.get(f"{HA}/api/states/sensor.p_sleep_duration").respond(json=st("x", "7.2", "h"))
    with caplog.at_level("INFO"):
        await health.fetch(db, cfg, TODAY)
    assert "rhr 52 (unchanged since 14 Oct)" in caplog.text and "sleep 7.2" in caplog.text


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


@respx.mock
async def test_fetch_logs_what_it_stored_and_skipped(tmp_path, ha_up, caplog):
    cfg = Config(health_rhr_entity="sensor.rhr", health_hrv_entity="sensor.hrv", health_sleep_entity="sensor.sleep")
    respx.get(f"{HA}/api/states/sensor.rhr").respond(json=st("x", "51", "bpm"))
    respx.get(f"{HA}/api/states/sensor.hrv").respond(json=st("x", "95", "ms", changed="2026-10-14T06:00:00+00:00"))
    respx.get(f"{HA}/api/states/sensor.sleep").respond(json=st("x", "unavailable", "min"))
    with caplog.at_level("INFO", logger="app.health"):
        await health.fetch(DB(tmp_path / "t.db"), cfg, TODAY)
    assert "Apple Health: stored rhr 51; skipped hrv (not reported today), sleep (no value)" in caplog.text


@respx.mock
async def test_notify_services_and_failed_notify_is_logged(ha_up, caplog):
    respx.get(f"{HA}/api/services").respond(json=[
        {"domain": "light", "services": {"turn_on": {}}},
        {"domain": "notify", "services": {"notify": {}, "send_message": {}, "mobile_app_johan_iphone": {}}}])
    assert await ha.notify_services() == ["notify.mobile_app_johan_iphone", "notify.notify"]
    respx.post(f"{HA}/api/services/notify/johan_iphone").respond(400)
    await ha.notify("notify.johan_iphone", "t", "m")
    assert "Notify via notify.johan_iphone failed: HTTP 400 (no such service" in caplog.text


def test_settings_flags_a_notify_service_ha_does_not_have(client, monkeypatch):
    services = ["notify.mobile_app_johan_iphone", "notify.notify"]

    async def fake_services():
        return services

    monkeypatch.setattr(ha, "notify_services", fake_services)
    client.post("/settings", data={"_section": "schedules", "notify_service": "notify.johan_iphone"})
    html = client.get("/settings").text
    assert "notify.johan_iphone doesn&#39;t exist in Home Assistant" in html or "notify.johan_iphone doesn't exist" in html
    assert 'name="notify_service" value="notify.mobile_app_johan_iphone"' in html
    assert '<option value="notify.notify">' in html
    main.app.state.st._ha_cache.clear()
    client.post("/settings", data={"_section": "schedules", "notify_service": "notify.mobile_app_johan_iphone"})
    assert "exist in Home Assistant" not in client.get("/settings").text


def test_notify_suggestion_only_when_unambiguous(client, monkeypatch):
    services = ["notify.mobile_app_anna_iphone", "notify.mobile_app_johan_iphone"]

    async def fake_services():
        return services

    monkeypatch.setattr(ha, "notify_services", fake_services)
    client.post("/settings", data={"_section": "schedules", "notify_service": "notify.johan_iphone"})
    assert 'name="notify_service" value="notify.mobile_app_johan_iphone"' in client.get("/settings").text
    main.app.state.st._ha_cache.clear()
    client.post("/settings", data={"_section": "schedules", "notify_service": "notify.old_phone"})
    html = client.get("/settings").text  # two phones, neither matches: keep what's stored, just warn
    assert 'name="notify_service" value="notify.old_phone"' in html and "Pick one from the list" in html


def test_hrv_threshold_is_the_athletes_own_swc():
    from app.health import hrv_threshold
    steady = [60, 62, 61, 59, 60, 63, 61, 60, 62, 59, 61, 60, 62, 61]   # Garmin's overnight rMSSD: ~2% spread
    noisy = [35, 60, 42, 75, 50, 38, 66, 45, 80, 40, 55, 70, 36, 58]    # Apple's spot SDNN
    assert hrv_threshold(steady) == -3.0          # a small spread: a small drop already counts (bounded at -3%)
    assert hrv_threshold(noisy) == -13.1          # a wide spread: only a big drop counts
    assert hrv_threshold([20, 90] * 7) == -25.0   # bounded: a few odd days can't make it absurd


def test_summary_gives_hrv_its_threshold_once_the_baseline_is_long_enough(tmp_path):
    from datetime import date, timedelta

    from app.db import DB
    from app.health import readiness, summary
    db, today = DB(tmp_path / "t.db"), date(2026, 10, 27)
    for i in range(35):
        db.set_health((today - timedelta(days=i)).isoformat(), "hrv", 60 + (i % 3) - (6 if i < 7 else 0), "ms")
    s = summary(db, today)["hrv"]
    assert s["drop_threshold_pct"] == -3.0 and s["change_pct"] < -3
    assert readiness({"hrv": s})["hrv"] == {"change_pct": s["change_pct"], "threshold_pct": -3.0, "signal": True}
    db2 = DB(tmp_path / "t2.db")
    for i in range(10):  # only a short baseline: no personal threshold yet
        db2.set_health((today - timedelta(days=i)).isoformat(), "hrv", 60, "ms")
    assert "drop_threshold_pct" not in summary(db2, today)["hrv"]
