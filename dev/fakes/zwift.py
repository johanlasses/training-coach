"""Fake Zwift: Keycloak password grant, profile and the zfiles API, speaking the same protobuf the app encodes."""
from __future__ import annotations

import sys

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

sys.path.insert(0, "/opt/app_src")  # the add-on's own protobuf helpers, so encoder and fake can't drift apart
from app.zwift import _field, _fields  # noqa: E402

app = FastAPI()
files: dict[int, dict] = {}  # id -> {filename, content}


@app.post("/token")
async def token(request: Request):
    form = await request.form()
    if form.get("grant_type") == "password" and form.get("password") == "wrong":
        return JSONResponse({"error": "invalid_grant"}, status_code=401)
    return {"access_token": "dev-zwift-access", "refresh_token": "dev-zwift-refresh", "expires_in": 3600,
            "refresh_expires_in": 30 * 86400}


@app.get("/api/profiles/me")
def me():
    return {"id": 42, "firstName": "Dev", "lastName": "Rider"}


@app.post("/api/zfiles")
async def upload(request: Request):
    folder = filename = content = None
    for num, val in _fields(await request.body()):
        if num == 2:
            folder = val.decode()
        elif num == 3:
            filename = val.decode()
        elif num == 4:
            content = val
    existing = next((i for i, f in files.items() if f["filename"] == filename), None)
    files[existing or len(files) + 1] = {"folder": folder, "filename": filename, "content": content}
    return Response(status_code=200)


@app.get("/api/zfiles/list")
def listing(folder: str = ""):
    out = b""
    for i, f in files.items():
        entry = _field(1, str(i).encode()) + _field(2, f["folder"].encode()) + _field(3, f["filename"].encode())
        out += _field(1, entry)
    return Response(out, media_type="application/x-protobuf-lite")


@app.get("/api/zfiles/{fid}/download")
def download(fid: int):
    return Response(files[fid]["content"])


@app.get("/_dev/files")
def dev_files():
    return {f["filename"]: f["content"].decode(errors="replace") for f in files.values()}
