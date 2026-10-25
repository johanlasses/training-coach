"""Fake Supervisor, in the three roles the add-on and the user's browser see it in:
 - /core/api/*: HA Core's API behind the Supervisor proxy (checks SUPERVISOR_TOKEN, forwards with a long-lived token)
 - /services/mqtt: the broker credentials
 - /api/hassio_ingress/dev/*: the ingress proxy (sets X-Ingress-Path like the real one)
 - /_dev/push: the sink for the fake iPhone's push notifications (HA's mobile_app POSTs them here)"""
from __future__ import annotations

import os
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

SUPERVISOR_TOKEN = os.environ["SUPERVISOR_TOKEN"]
HA = os.environ.get("HA_CORE_URL", "http://homeassistant:8123")
ADDON = os.environ.get("ADDON_URL", "http://addon:8099")
INGRESS = "/api/hassio_ingress/dev"
TOKEN_FILE = Path("/state/ha_token")

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


@app.api_route(INGRESS + "/{path:path}", methods=["GET", "POST"])
@app.api_route(INGRESS, methods=["GET"])
async def ingress(request: Request, path: str = ""):
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP}
    headers |= {"X-Ingress-Path": INGRESS, "X-Hass-Source": "core.ingress", "X-Forwarded-For": request.client.host,
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
