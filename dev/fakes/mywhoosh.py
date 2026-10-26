"""Fake MyWhoosh: the app's login and the coaching API's custom-workout upload, with the checks the real one makes
(numeric SportsModeType 0 at the top). Password "wrong" fails; /_dev/workouts shows what arrived, keyed by Id."""
from __future__ import annotations

import base64
import json
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()
workouts: dict[str, dict] = {}
TOKEN_PAYLOAD = base64.urlsafe_b64encode(json.dumps({"exp": int(time.time()) + 30 * 86400}).encode()).decode().rstrip("=")
TOKEN = f"eyJhbGciOiJIUzI1NiJ9.{TOKEN_PAYLOAD}.dev"


@app.post("/http-service/api/login")
async def login(request: Request):
    body = await request.json()
    if body.get("Password") == "wrong" or body.get("Action") != 1001:
        return JSONResponse({"Success": False, "Message": "Invalid email or password"}, status_code=400)
    who = "dev-whoosh-" + str(abs(hash(body.get("Username", ""))) % 1000)  # one account per email
    return {"Success": True, "Verified": True, "WhooshId": who, "AccessToken": TOKEN, "RefreshToken": "dev-r"}


@app.post("/api/v2/client/custom-workout-upload")
async def upload(request: Request):
    if request.headers.get("Authorization") != f"Bearer {TOKEN}":
        return JSONResponse({"message": "Unauthorized"}, status_code=401)
    body = await request.json()
    if body.get("SportsModeType") not in (0, 1, 2, 3) or not isinstance(body.get("SportsModeType"), int):
        return JSONResponse({"message": "SportsModeType must be one of 0, 1, 2, 3"}, status_code=400)
    for w in body.get("WorkoutsData") or []:
        if w.get("StepCount") != len(w.get("WorkoutStepsArray") or []) or set(w.get("WorkoutSteps") or {}) != {
                str(s["Id"]) for s in w["WorkoutStepsArray"]}:
            return JSONResponse({"message": "Invalid workout"}, status_code=400)
        workouts[f"{body.get('UserId')}/{w['Id']}"] = w  # the same Id replaces, per account
    return {"message": "Custom workout is being uploaded.", "status": True}


@app.get("/_dev/workouts")
def dev_workouts():
    return {k: {"Name": w["Name"], "StepCount": w["StepCount"], "Time": w["Time"],
                     "messages": sum(len(s["WorkoutMessage"]) for s in w["WorkoutStepsArray"])} for k, w in workouts.items()}


@app.post("/_dev/reset")
def reset():
    workouts.clear()
    return {}
