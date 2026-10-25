"""Fake Supervisor, in the three roles the add-on and the user's browser see it in:
 - /core/api/*: HA Core's API behind the Supervisor proxy (checks SUPERVISOR_TOKEN, forwards with a long-lived token)
 - /core/websocket: Core's websocket API, the same way (the add-on asks it who HA's owner is)
 - /services/mqtt: the broker credentials
 - /api/hassio_ingress/dev/*: the ingress proxy (sets X-Ingress-Path and X-Remote-User-* like the real one; the user
   is the `tc_dev_user` cookie, `dev` by default: /_dev/as/dev2 switches the browser to the second athlete)
 - /_dev/push: the sink for the fake iPhone's push notifications (HA's mobile_app POSTs them here)"""
from __future__ import annotations

import os
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, Response
import asyncio
import json

import websockets
from fastapi import WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse

SUPERVISOR_TOKEN = os.environ["SUPERVISOR_TOKEN"]
HA = os.environ.get("HA_CORE_URL", "http://homeassistant:8123")
ADDON = os.environ.get("ADDON_URL", "http://addon:8099")
INGRESS = "/api/hassio_ingress/dev"
TOKEN_FILE, USERS_FILE = Path("/state/ha_token"), Path("/state/users.json")
REMOTE_USER = {"x-remote-user-id", "x-remote-user-name", "x-remote-user-display-name"}

app = FastAPI()
pushes: list[dict] = []
HOP = {"host", "content-length", "transfer-encoding", "connection", "keep-alive", "upgrade"}


@app.api_route("/core/api/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def core(path: str, request: Request):
    if request.headers.get("authorization") != f"Bearer {SUPERVISOR_TOKEN}":
        return JSONResponse({"result": "error", "message": "Unauthorized"}, status_code=401)
    if not TOKEN_FILE.exists():
        return JSONResponse({"message": "dev stack not bootstrapped (no HA token yet)"}, status_code=503)
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.request(request.method, f"{HA}/api/{path}", params=request.query_params, content=await request.body(),
                            headers={"Authorization": f"Bearer {TOKEN_FILE.read_text().strip()}",
                                     "Content-Type": request.headers.get("content-type", "application/json")})
    return Response(r.content, status_code=r.status_code, media_type=r.headers.get("content-type"))


@app.websocket("/core/websocket")
async def core_ws(client: WebSocket):
    """Like Supervisor: authenticates the add-on's token itself, then relays to Core as an admin. supervisor/ and
    hassio/ commands are refused, as the real proxy does."""
    await client.accept()
    await client.send_json({"type": "auth_required", "ha_version": "dev"})
    auth = await client.receive_json()
    if auth.get("access_token") != SUPERVISOR_TOKEN:
        await client.send_json({"type": "auth_invalid", "message": "Invalid access"})
        return await client.close()
    async with websockets.connect(HA.replace("http", "ws", 1) + "/api/websocket", max_size=None) as core:
        await core.recv()
        await core.send(json.dumps({"type": "auth", "access_token": TOKEN_FILE.read_text().strip()}))
        await client.send_text(await core.recv())

        async def up():
            while True:
                msg = await client.receive_json()
                if str(msg.get("type", "")).startswith(("supervisor/", "hassio/")):
                    await client.send_json({"id": msg.get("id"), "type": "result", "success": False,
                                            "error": {"code": "unauthorized", "message": "Unauthorized"}})
                else:
                    await core.send(json.dumps(msg))

        async def down():
            async for msg in core:
                await client.send_text(msg)

        tasks = [asyncio.create_task(up()), asyncio.create_task(down())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except WebSocketDisconnect:
            pass
        finally:
            for t in tasks:
                t.cancel()


@app.get("/services/mqtt")
def mqtt(request: Request):
    if request.headers.get("authorization") != f"Bearer {SUPERVISOR_TOKEN}":
        return JSONResponse({"result": "error"}, status_code=401)
    return {"result": "ok", "data": {"host": "mosquitto", "port": 1883, "ssl": False, "protocol": "3.1.1",
                                     "username": "dev", "password": "dev"}}


@app.post("/_dev/push")
async def push(request: Request):
    pushes.append(await request.json())
    return {"sentPushNotifications": 1, "errors": []}


@app.get("/_dev/push")
def list_pushes():
    return pushes


@app.delete("/_dev/push")
def clear_pushes():
    pushes.clear()
    return {"ok": True}


@app.get("/_dev/as/{user}")
def act_as(user: str):
    r = RedirectResponse(INGRESS + "/")
    r.set_cookie("tc_dev_user", user)
    return r


def remote_user(request: Request) -> dict:
    """What Supervisor adds for the HA user behind the ingress session. An unknown name (e.g. `stranger`) gets a
    made-up id, like a user the add-on's owner hasn't let in."""
    name = request.cookies.get("tc_dev_user", "dev")
    users = json.loads(USERS_FILE.read_text()) if USERS_FILE.exists() else {}
    u = users.get(name) or {"id": name.encode().hex()[:32].ljust(32, "0"), "name": name.title()}
    return {"X-Remote-User-Id": u["id"], "X-Remote-User-Name": name, "X-Remote-User-Display-Name": u["name"]}


@app.api_route(INGRESS + "/{path:path}", methods=["GET", "POST"])
@app.api_route(INGRESS, methods=["GET"])
async def ingress(request: Request, path: str = ""):
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP | REMOTE_USER}
    headers |= remote_user(request) | {"X-Ingress-Path": INGRESS, "X-Hass-Source": "core.ingress", "X-Forwarded-For": request.client.host,
                "X-Forwarded-Proto": "http"}
    client = httpx.AsyncClient(timeout=httpx.Timeout(120, connect=5))
    req = client.build_request(request.method, f"{ADDON}/{path}", params=request.query_params, headers=headers,
                               content=await request.body())
    try:
        r = await client.send(req, stream=True)
    except httpx.HTTPError as e:
        await client.aclose()
        return JSONResponse({"error": f"add-on unreachable: {e}"}, status_code=502)

    async def body():
        try:
            async for chunk in r.aiter_raw():
                yield chunk
        finally:
            await r.aclose()
            await client.aclose()

    return StreamingResponse(body(), status_code=r.status_code,
                             headers={k: v for k, v in r.headers.items() if k.lower() not in HOP | {"content-encoding"}})
