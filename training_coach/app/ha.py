"""Home Assistant access: notifications and state reads via the Supervisor proxy, sensors via MQTT."""
from __future__ import annotations

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
    except httpx.HTTPError as e:
        log.warning("Notify failed: %s", e)


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
