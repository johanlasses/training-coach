"""Brings a fresh dev HA to the state the real one is in: onboarded, a long-lived token, MQTT connected, and a
Dev iPhone registered through the real mobile_app integration with Apple-Health-style sensors. Idempotent.

Usage: bootstrap.py [health]   (`health` only pushes today's sensor values, like the iOS app does)"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx
from websockets.sync.client import connect

HA = "http://localhost:8123"
STATE = Path(__file__).parent / ".state"
TOKEN_FILE, MOBILE_FILE = STATE / "ha_token", STATE / "mobile.json"
CLIENT_ID = f"{HA}/"
USER, PASSWORD = "dev", "dev-password-1"
PUSH_URL = "http://supervisor/_dev/push"

# unique_id: (name, state, unit, icon): names and units as the HA iOS app reports them
SENSORS = {
    "resting_heart_rate": ("Resting Heart Rate", 52, "bpm", "mdi:heart-pulse"),
    "heart_rate_variability": ("Heart Rate Variability", 68, "ms", "mdi:heart-flash"),
    "sleep_duration": ("Sleep Duration", 7.4, "hr", "mdi:sleep"),
    "weight": ("Weight", 78.5, "kg", "mdi:scale-bathroom"),
    "vo2_max": ("VO2 Max", 51, "ml/kg/min", "mdi:lungs"),
}


def log(msg: str) -> None:
    print(f"[bootstrap] {msg}", flush=True)


def wait_for_ha() -> None:
    for _ in range(120):
        try:
            if httpx.get(f"{HA}/api/onboarding", timeout=3).status_code in (200, 404):
                return
        except httpx.HTTPError:
            pass
        time.sleep(2)
    sys.exit("Home Assistant did not come up on :8123")


def access_token() -> str:
    """A short-lived token: via the onboarding user step on a fresh HA, else by logging in."""
    c = httpx.Client(base_url=HA, timeout=20)
    steps = {s["step"]: s["done"] for s in c.get("/api/onboarding").json()} if c.get("/api/onboarding").status_code == 200 else {}
    if steps and not steps.get("user", True):
        log("onboarding the dev user")
        code = c.post("/api/onboarding/users", json={"client_id": CLIENT_ID, "name": "Dev", "username": USER,
                                                     "password": PASSWORD, "language": "en"}).json()["auth_code"]
    else:
        flow = c.post("/auth/login_flow", json={"client_id": CLIENT_ID, "handler": ["homeassistant", None],
                                                "redirect_uri": CLIENT_ID}).json()
        r = c.post(f"/auth/login_flow/{flow['flow_id']}", json={"username": USER, "password": PASSWORD,
                                                                 "client_id": CLIENT_ID})
        if r.json().get("type") != "create_entry":
            sys.exit(f"login failed: {r.text}. Run `dev/devha.sh reset` for a clean HA.")
        code = r.json()["result"]
    tok = c.post("/auth/token", data={"grant_type": "authorization_code", "code": code, "client_id": CLIENT_ID}).json()["access_token"]
    if steps and not steps.get("user", True):
        h = {"Authorization": f"Bearer {tok}"}
        for step, body in (("core_config", {}), ("analytics", {}), ("integration", {"client_id": CLIENT_ID, "redirect_uri": CLIENT_ID})):
            c.post(f"/api/onboarding/{step}", json=body, headers=h)
    return tok


def long_lived_token() -> str:
    if TOKEN_FILE.exists():
        t = TOKEN_FILE.read_text().strip()
        if httpx.get(f"{HA}/api/", headers={"Authorization": f"Bearer {t}"}, timeout=5).status_code == 200:
            return t
    short = access_token()
    with connect("ws://localhost:8123/api/websocket") as ws:
        ws.recv()
        ws.send(json.dumps({"type": "auth", "access_token": short}))
        assert json.loads(ws.recv())["type"] == "auth_ok"
        ws.send(json.dumps({"id": 1, "type": "auth/long_lived_access_token", "client_name": f"dev-{int(time.time())}",
                            "lifespan": 3650}))
        t = json.loads(ws.recv())["result"]
    STATE.mkdir(exist_ok=True)
    TOKEN_FILE.write_text(t)
    log(f"long-lived token written to {TOKEN_FILE}")
    return t


def mqtt(h: dict) -> None:
    c = httpx.Client(base_url=HA, headers=h, timeout=30)
    if any(e["domain"] == "mqtt" for e in c.get("/api/config/config_entries/entry").json()):
        return
    log("adding the MQTT integration")
    flow = c.post("/api/config/config_entries/flow", json={"handler": "mqtt", "show_advanced_options": False}).json()
    flow = c.post(f"/api/config/config_entries/flow/{flow['flow_id']}", json={
        "broker": "mosquitto", "port": 1883, "protocol": "3.1.1", "username": "dev", "password": "dev",
        "other_settings": {"set_client_cert": False, "set_ca_cert": "off", "transport": "tcp"}}).json()
    if flow.get("type") != "create_entry":
        sys.exit(f"MQTT setup failed: {flow}")


def webhook(webhook_id: str, type_: str, data) -> httpx.Response:
    return httpx.post(f"{HA}/api/webhook/{webhook_id}", json={"type": type_, "data": data}, timeout=20)


def iphone(h: dict) -> str:
    if MOBILE_FILE.exists():
        return json.loads(MOBILE_FILE.read_text())["webhook_id"]
    log("registering Dev iPhone (mobile_app)")
    r = httpx.post(f"{HA}/api/mobile_app/registrations", headers=h, timeout=20, json={
        "device_id": "dev-iphone-0001", "app_id": "io.robbie.HomeAssistant", "app_name": "Home Assistant",
        "app_version": "2026.10.1", "device_name": "Dev iPhone", "manufacturer": "Apple", "model": "iPhone16,1",
        "os_name": "iOS", "os_version": "26.0", "supports_encryption": False,
        "app_data": {"push_url": PUSH_URL, "push_token": "dev-push-token"}})
    r.raise_for_status()
    wid = r.json()["webhook_id"]
    MOBILE_FILE.write_text(json.dumps({"webhook_id": wid}))
    for uid, (name, state, unit, icon) in SENSORS.items():
        webhook(wid, "register_sensor", {"type": "sensor", "unique_id": uid, "name": name, "state": state,
                                         "unit_of_measurement": unit, "icon": icon, "state_class": "measurement"}).raise_for_status()
    return wid


def push_health(wid: str) -> None:
    webhook(wid, "update_sensor_states", [{"type": "sensor", "unique_id": uid, "state": state, "icon": icon}
                                          for uid, (_, state, _, icon) in SENSORS.items()]).raise_for_status()


if __name__ == "__main__":
    wait_for_ha()
    token = long_lived_token()
    h = {"Authorization": f"Bearer {token}"}
    if sys.argv[1:] == ["health"]:
        push_health(iphone(h))
        sys.exit(log("pushed today's health values"))
    mqtt(h)
    wid = iphone(h)
    push_health(wid)
    log("done: HA at http://localhost:8123 (dev / dev-password-1), token in dev/.state/ha_token")
