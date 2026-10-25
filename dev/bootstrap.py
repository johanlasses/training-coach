"""Brings a fresh dev HA to the state the real one is in: onboarded, a long-lived token, MQTT connected, and a
Dev iPhone registered through the real mobile_app integration with Apple-Health-style sensors. Idempotent.
Also a second athlete like the real box's friends: `dev2`, a non-admin in HA's read-only group, with Dev iPhone 2.
The HA user ids go to .state/users.json for the fake ingress, which sends them as X-Remote-User-* like Supervisor.

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
TOKEN_FILE, MOBILE_FILE, USERS_FILE = STATE / "ha_token", STATE / "mobile.json", STATE / "users.json"
CLIENT_ID = f"{HA}/"
USER, PASSWORD = "dev", "dev-password-1"
USER2, PASSWORD2, NAME2 = "dev2", "dev2-password-1", "Dev Two"
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


def login(username: str, password: str) -> str:
    """A short-lived token for an existing user."""
    c = httpx.Client(base_url=HA, timeout=20)
    flow = c.post("/auth/login_flow", json={"client_id": CLIENT_ID, "handler": ["homeassistant", None],
                                            "redirect_uri": CLIENT_ID}).json()
    r = c.post(f"/auth/login_flow/{flow['flow_id']}", json={"username": username, "password": password, "client_id": CLIENT_ID})
    if r.json().get("type") != "create_entry":
        sys.exit(f"login as {username} failed: {r.text}. Run `dev/devha.sh reset` for a clean HA.")
    return c.post("/auth/token", data={"grant_type": "authorization_code", "code": r.json()["result"],
                                       "client_id": CLIENT_ID}).json()["access_token"]


def ws_calls(token: str, *msgs: dict) -> list:
    with connect("ws://localhost:8123/api/websocket") as ws:
        ws.recv()
        ws.send(json.dumps({"type": "auth", "access_token": token}))
        assert json.loads(ws.recv())["type"] == "auth_ok"
        out = []
        for i, m in enumerate(msgs, 1):
            ws.send(json.dumps({"id": i} | m))
            r = json.loads(ws.recv())
            if not r.get("success"):
                sys.exit(f"{m['type']} failed: {r}")
            out.append(r["result"])
        return out


def wait_running(token: str) -> None:
    """HTTP answers before every integration is loaded (person/create is "unknown" until then)."""
    for _ in range(90):
        r = httpx.get(f"{HA}/api/config", headers={"Authorization": f"Bearer {token}"}, timeout=5)
        if r.status_code == 200 and r.json().get("state") == "RUNNING":
            return
        time.sleep(2)
    sys.exit("Home Assistant did not finish starting")


def users(token: str) -> dict:
    """The owner's and dev2's HA user ids; creates dev2 (read-only group, own person) the first time."""
    wait_running(token)
    listed = {u["name"]: u for u in ws_calls(token, {"type": "config/auth/list"})[0]}
    if NAME2 not in listed:
        log(f"creating {USER2}: a non-admin in the read-only group, with a person")
        uid = ws_calls(token, {"type": "config/auth/create", "name": NAME2, "group_ids": ["system-read-only"]})[0]["user"]["id"]
        ws_calls(token, {"type": "config/auth_provider/homeassistant/create", "user_id": uid, "username": USER2,
                         "password": PASSWORD2},
                 {"type": "person/create", "name": NAME2, "user_id": uid})
        listed[NAME2] = {"id": uid}
    out = {USER: {"id": listed["Dev"]["id"], "name": "Dev"}, USER2: {"id": listed[NAME2]["id"], "name": NAME2}}
    USERS_FILE.write_text(json.dumps(out))
    return out


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


def iphone(h: dict, n: int = 1) -> str:
    """Dev iPhone (n=1, the owner's) or Dev iPhone 2 (dev2's), registered with that user's token like the app does."""
    known = json.loads(MOBILE_FILE.read_text()) if MOBILE_FILE.exists() else {}
    key = "webhook_id" if n == 1 else f"webhook_id_{n}"
    if key in known:
        return known[key]
    name = "Dev iPhone" if n == 1 else f"Dev iPhone {n}"
    log(f"registering {name} (mobile_app)")
    r = httpx.post(f"{HA}/api/mobile_app/registrations", headers=h, timeout=20, json={
        "device_id": f"dev-iphone-000{n}", "app_id": "io.robbie.HomeAssistant", "app_name": "Home Assistant",
        "app_version": "2026.10.1", "device_name": name, "manufacturer": "Apple", "model": "iPhone16,1",
        "os_name": "iOS", "os_version": "26.0", "supports_encryption": False,
        "app_data": {"push_url": PUSH_URL, "push_token": f"dev-push-token-{n}" if n > 1 else "dev-push-token"}})
    r.raise_for_status()
    wid = r.json()["webhook_id"]
    MOBILE_FILE.write_text(json.dumps(known | {key: wid}))
    for uid, (name, state, unit, icon) in SENSORS.items():
        webhook(wid, "register_sensor", {"type": "sensor", "unique_id": uid, "name": name, "state": state,
                                         "unit_of_measurement": unit, "icon": icon, "state_class": "measurement"}).raise_for_status()
    return wid


def push_health(wid: str, offset: float = 0) -> None:
    """`offset` sets Dev iPhone 2's values apart, so a mix-up between the athletes shows."""
    webhook(wid, "update_sensor_states", [{"type": "sensor", "unique_id": uid, "state": state + offset, "icon": icon}
                                          for uid, (_, state, _, icon) in SENSORS.items()]).raise_for_status()


if __name__ == "__main__":
    wait_for_ha()
    token = long_lived_token()
    h = {"Authorization": f"Bearer {token}"}
    if sys.argv[1:] == ["health"]:
        push_health(iphone(h))
        push_health(iphone({}, 2), offset=10)
        sys.exit(log("pushed today's health values"))
    mqtt(h)
    users(token)
    push_health(iphone(h))
    wid2 = iphone({"Authorization": f"Bearer {login(USER2, PASSWORD2)}"}, 2)
    push_health(wid2, offset=10)
    log(f"done: HA at http://localhost:8123 ({USER} / {PASSWORD}, or {USER2} / {PASSWORD2}), token in dev/.state/ha_token")
