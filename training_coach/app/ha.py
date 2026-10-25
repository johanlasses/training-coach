"""Home Assistant access: notifications and state reads via the Supervisor proxy, sensors via MQTT."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket

import httpx

log = logging.getLogger(__name__)

SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN")
# Local dev: HA_URL=http://192.168.68.156:8123 HA_TOKEN=<long-lived token>
HA_URL = "http://supervisor/core" if SUPERVISOR_TOKEN else os.environ.get("HA_URL", "")
HA_TOKEN = SUPERVISOR_TOKEN or os.environ.get("HA_TOKEN", "")


def available() -> bool:
    return bool(HA_URL and HA_TOKEN)


async def _call(method: str, path: str, body: dict | None = None, timeout: float = 20) -> httpx.Response:
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=3)) as c:
        r = await c.request(method, f"{HA_URL}/api{path}", json=body,
                            headers={"Authorization": f"Bearer {HA_TOKEN}"})
        r.raise_for_status()
        return r


async def notify(service: str, title: str, message: str, data: dict | None = None) -> None:
    """`data` goes to the companion app as is, e.g. {"url": ...} to open a page when the notification is tapped.
    Only notify.* services get it: others (persistent_notification.create) reject unknown keys."""
    if not available() or not service or "." not in service:
        return
    domain, name = service.split(".", 1)
    body = {"title": title, "message": message} | ({"data": data} if data and domain == "notify" else {})
    try:
        await _call("POST", f"/services/{domain}/{name}", body)
    except httpx.HTTPStatusError as e:  # HA's 400 has no useful body: an unknown service or data it rejects
        log.warning("Notify via %s failed: HTTP %s%s", service, e.response.status_code,
                    " (no such service in Home Assistant? Pick one on Settings)" if e.response.status_code == 400 else "")
    except httpx.HTTPError as e:
        log.warning("Notify via %s failed: %s", service, e)


async def notify_services() -> list[str] | None:
    """The notify.* services HA has, phones (mobile_app_*) first; None when HA can't be read. notify.send_message
    is left out: it needs a notify entity, not a title and message."""
    if not available():
        return None
    try:
        domains = (await _call("GET", "/services", timeout=8)).json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("Reading HA services failed: %s", e)
        return None
    names = next((d.get("services") or {} for d in domains if d.get("domain") == "notify"), {})
    return sorted((f"notify.{n}" for n in names if n != "send_message"),
                  key=lambda s: (not s.startswith("notify.mobile_app_"), s))


def addon_slug() -> str:
    """This add-on's slug, e.g. local_training_coach or 1a2b3c4d_training_coach. Supervisor names the container's
    host after it with dashes, so it's read from there: no Supervisor API role needed."""
    return socket.gethostname().replace("-", "_")


def panel_url(path: str = "/") -> str | None:
    """Where the companion app should go to show `path` of this add-on (its ingress panel). None outside HA."""
    return f"/hassio/ingress/{addon_slug()}{path}" if SUPERVISOR_TOKEN else None


def tap(path: str, thread: str = "training") -> dict:
    """Notification data: open `path` in the add-on on tap, and group the notifications per thread on iOS."""
    url = panel_url(path)
    return ({"url": url, "clickAction": url} if url else {}) | {"push": {"thread-id": thread}, "group": thread}


async def states() -> list[dict]:
    """All entity states, or [] when HA isn't reachable."""
    if not available():
        return []
    try:
        return (await _call("GET", "/states", timeout=8)).json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("Reading HA states failed: %s", e)
        return []


async def owner_user_id() -> str | None:
    """The id of Home Assistant's owner (the user who set HA up), from Core's websocket API: Supervisor proxies it for
    add-ons with homeassistant_api, as its own admin user. None when HA can't be asked (not started yet, local runs)."""
    if not available():
        return None
    import websockets

    url = HA_URL.replace("http", "ws", 1) + ("/websocket" if SUPERVISOR_TOKEN else "/api/websocket")
    async def ask() -> dict | None:
        async with websockets.connect(url, open_timeout=5, max_size=None) as ws:
            await ws.recv()  # auth_required
            await ws.send(json.dumps({"type": "auth", "access_token": HA_TOKEN}))
            if json.loads(await ws.recv()).get("type") != "auth_ok":
                return None
            await ws.send(json.dumps({"id": 1, "type": "config/auth/list"}))
            return json.loads(await ws.recv())

    try:
        r = await asyncio.wait_for(ask(), 10) or {}  # a half-started Core mustn't hold up the add-on's start
    except (OSError, TimeoutError, websockets.WebSocketException, ValueError) as e:
        log.info("Asking HA for its owner failed: %s", e)
        return None
    return next((u["id"] for u in r.get("result") or [] if u.get("is_owner")), None)


async def user_devices(user_id: str) -> list[str] | None:
    """The phones a Home Assistant user signed in to with the companion app, as slugs: the device trackers HA adds to
    their person, e.g. johans_iphone for device_tracker.johans_iphone. The phone's sensors are sensor.<slug>_* and its
    notify service notify.mobile_app_<slug>. None when HA can't be read."""
    all_states = await states()
    if not all_states:
        return None
    return sorted({t.removeprefix("device_tracker.") for s in all_states if s.get("entity_id", "").startswith("person.")
                   and (s.get("attributes") or {}).get("user_id") == user_id
                   for t in s["attributes"].get("device_trackers") or [] if t.startswith("device_tracker.")})


async def state(entity_id: str) -> dict | None:
    if not available() or not entity_id:
        return None
    try:
        return (await _call("GET", f"/states/{entity_id}", timeout=8)).json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("Reading %s failed: %s", entity_id, e)
        return None


# --- MQTT sensors --------------------------------------------------------

SENSORS = {
    # key: (name, unit, icon, device_class, state_class)
    "ftp": ("FTP", "W", "mdi:lightning-bolt", "power", "measurement"),
    "wkg": ("FTP W/kg", "W/kg", "mdi:scale-bathroom", None, "measurement"),
    "ctl": ("Fitness (CTL)", None, "mdi:chart-line", None, "measurement"),
    "atl": ("Fatigue (ATL)", None, "mdi:chart-line-variant", None, "measurement"),
    "tsb": ("Form (TSB)", None, "mdi:scale-balance", None, "measurement"),
    "week_hours": ("Hours this week", "h", "mdi:timer-outline", None, None),
    "week_km": ("Km this week", "km", "mdi:map-marker-distance", None, None),
    "today_session": ("Today's session", None, "mdi:bike", None, None),
    "last_ride": ("Last ride", None, "mdi:message-text-outline", None, None),
}
STATE_TOPIC = "training_coach/state"


async def mqtt_settings() -> dict | None:
    """Broker credentials from the Supervisor services API, or MQTT_* env for local dev."""
    if SUPERVISOR_TOKEN:
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get("http://supervisor/services/mqtt",
                                headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}"})
                r.raise_for_status()
                return r.json()["data"]
        except (httpx.HTTPError, KeyError) as e:
            log.info("MQTT service not available: %s", e)
            return None
    if os.environ.get("MQTT_HOST"):
        return {"host": os.environ["MQTT_HOST"], "port": int(os.environ.get("MQTT_PORT", 1883)),
                "username": os.environ.get("MQTT_USER"), "password": os.environ.get("MQTT_PASSWORD")}
    return None


async def publish_sensors(state: dict) -> None:
    settings = await mqtt_settings()
    if not settings:
        return
    import aiomqtt

    device = {"identifiers": ["training_coach"], "name": "Training Coach", "manufacturer": "Training Coach add-on"}
    try:
        async with aiomqtt.Client(hostname=settings["host"], port=int(settings.get("port", 1883)),
                                  username=settings.get("username"), password=settings.get("password")) as c:
            for key, (name, unit, icon, dev_class, state_class) in SENSORS.items():
                cfg = {
                    "name": name,
                    "unique_id": f"training_coach_{key}",
                    "default_entity_id": f"sensor.training_{key}",  # object_id is ignored since HA 2026.4
                    "state_topic": STATE_TOPIC,
                    "value_template": f"{{{{ value_json.{key} }}}}",
                    "icon": icon,
                    "device": device,
                }
                if unit:
                    cfg["unit_of_measurement"] = unit
                if dev_class:
                    cfg["device_class"] = dev_class
                if state_class:
                    cfg["state_class"] = state_class
                await c.publish(f"homeassistant/sensor/training_coach/{key}/config", json.dumps(cfg), retain=True)
            await c.publish(STATE_TOPIC, json.dumps(state), retain=True)
    except Exception as e:  # noqa: BLE001 - sensors are best-effort, never break the app
        log.warning("MQTT publish failed: %s", e)
