"""Send planned workouts to the athlete's MyWhoosh account ("My Workouts"), the free alternative to Zwift.

MyWhoosh has no public API. This talks to the endpoints its apps use, as documented by the MyWhoosh community
(github.com/mywhoosh-community/mywhoosh-api, MIT) and a working client (isllabres/mywhoosh-mcp-server):

- Login: POST services.mywhoosh.com/http-service/api/login with the account's email and password once; the answer has
  WhooshId and an access token (a JWT). Only the token is kept, never the password. No refresh is documented: when the
  token runs out, the athlete signs in again on Profile.
- Upload: POST coaching.mywhoosh.com/api/v2/client/custom-workout-upload. Power is a fraction of FTP (MyWhoosh scales it
  to the FTP set in the app), times in seconds, ramps as E_WarmUp/E_CoolDown, repeats as steps sharing an IntervalId.
  SportsModeType must be the number 0 (cycling), at the top and per workout, or MyWhoosh answers 400.
- Sending again with the same workout Id updates it, so each plan day gets a stable Id: re-planning or re-sending a
  day replaces the workout instead of adding a copy. (MyWhoosh can't delete or read back workouts uploaded this way,
  a backend bug, so a copy could only be removed in the app.)
- "You don't have enough credit!" means the account has no room for more custom workouts: the athlete deletes some
  in the app.
- Calendar: POST service14.mywhoosh.com/v1/task/create with TaskType E_Simple_Workout, TaskTypeId = the workout Id (a
  string), start/end as epoch seconds; the reply carries data.TaskId, and DELETE /v1/task/<TaskId> removes it. Also
  from the community client, and unverified against the real service like the favourite flag (IsFavorite on upload):
  a refused favourite upload is retried without it.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
import uuid
import zlib
from datetime import date, datetime, timedelta
from datetime import time as clock

import httpx

from . import workouts
from .db import DB

log = logging.getLogger(__name__)

# TC_MYWHOOSH_* point the app at a fake MyWhoosh (dev/ stack); unset in production
LOGIN_URL = os.environ.get("TC_MYWHOOSH_LOGIN_URL", "https://services.mywhoosh.com/http-service/api/login")
UPLOAD_URL = os.environ.get("TC_MYWHOOSH_UPLOAD_URL", "https://coaching.mywhoosh.com/api/v2/client/custom-workout-upload")
TASK_URL = os.environ.get("TC_MYWHOOSH_TASK_URL", "https://service14.mywhoosh.com/v1/task")  # the calendar
CALENDAR_AT = clock(20, 0)  # when the session sits on MyWhoosh's calendar; the athlete rides it whenever
HEADERS = {"Content-Type": "application/json", "User-Agent": "TrainingCoach/1.0 (Home Assistant add-on)"}
ID_BASE = 9_000_000_000_000  # stable workout Ids, far from MyWhoosh's own timestamp-like Ids (see workout_id)
TEST_ERG = "E_OFF"           # tests and free rides leave the resistance free; everything else is ERG


class MyWhooshError(Exception):
    pass


class MyWhooshClient:
    def __init__(self, db: DB, http: httpx.AsyncClient | None = None):
        self.db = db
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10), headers=HEADERS)

    @property
    def configured(self) -> bool:
        """A token that hasn't run out. MyWhoosh may still refuse it; uploads report that."""
        tok = self.db.get("mywhoosh_token") or {}
        return bool(tok.get("access_token")) and tok.get("expires_at", float("inf")) > time.time() + 60

    def account(self) -> dict | None:
        return self.db.get("mywhoosh_account")

    def disconnect(self) -> None:
        self.db.set("mywhoosh_token", None)

    def device_id(self) -> str:
        """One device id per athlete, like an installed app."""
        if not (d := self.db.get("mywhoosh_device")):
            d = str(uuid.uuid4())
            self.db.set("mywhoosh_device", d)
        return d

    async def connect(self, email: str, password: str) -> dict:
        r = await self.http.post(LOGIN_URL, json={
            "Username": email.strip(), "Password": password, "Platform": "Android", "Action": 1001,
            "CorrelationId": str(uuid.uuid4()), "DeviceId": self.device_id(), "Authorization": ""})
        data = _body(r)
        # Without a WhooshId every upload would go to UserId "" and fail without saying why
        if r.status_code >= 400 or not data.get("Success") or not data.get("AccessToken") or not data.get("WhooshId"):
            log.warning("MyWhoosh login failed: %s %s", r.status_code, str(data.get("Message") or r.text)[:200])
            raise MyWhooshError(data.get("Message") or f"MyWhoosh login failed ({r.status_code})")
        token = data["AccessToken"]
        self.db.set("mywhoosh_token", {"access_token": token, "whoosh_id": str(data.get("WhooshId") or ""),
                                       "expires_at": jwt_exp(token) or time.time() + 30 * 86400})
        account = {"email": email.strip(), "whoosh_id": str(data.get("WhooshId") or "")}
        self.db.set("mywhoosh_account", account)
        return account

    async def upload_workout(self, day: str, plan_day: dict, name: str, cheers: tuple[str, ...] = (),
                             last_rep: str = "") -> int:
        """Puts the session in My Workouts, replacing the one sent for that day before. Returns the workout Id."""
        tok = self.db.get("mywhoosh_token") or {}
        if not tok.get("access_token"):
            raise MyWhooshError("MyWhoosh is not connected — sign in on Profile")
        workout = to_workout(day, plan_day, name, cheers, last_rep, tok["whoosh_id"])

        async def send(w: dict) -> httpx.Response:
            return await self.http.post(UPLOAD_URL, headers={"Authorization": f"Bearer {tok['access_token']}"},
                                        json={"UserId": tok["whoosh_id"], "SportsModeType": 0, "WorkoutsData": [w]})

        r = await send(workout)
        if r.status_code not in (200, 401) or _body(r).get("status") is False and "credit" not in r.text.lower():
            # IsFavorite on upload is unverified: if MyWhoosh refuses the workout, send it again as before (0.10.2)
            log.info("MyWhoosh refused the favourite upload (%s %s); sending without it", r.status_code, r.text[:200])
            workout["IsFavorite"] = False
            r = await send(workout)
        if r.status_code == 401:
            self.db.set("mywhoosh_token", tok | {"expires_at": 0})  # show as signed out
            raise MyWhooshError("The MyWhoosh login has expired — sign in again on Profile")
        data = _body(r)
        # Only an explicit yes counts: a maintenance page or {"Success": false} must not read as sent
        if r.status_code >= 400 or not (data.get("status") is True or data.get("Success") is True):
            message = str(data.get("message") or data.get("Message") or r.text)[:200]
            log.warning("MyWhoosh upload failed: %s %s", r.status_code, message)
            if "enough credit" in message.lower():
                raise MyWhooshError("MyWhoosh has no room for more custom workouts: delete a few in the MyWhoosh app "
                                    "(My Workouts), then send again")
            raise MyWhooshError(f"MyWhoosh refused the workout ({r.status_code}{': ' + message if message else ''})")
        return workout["Id"]

    async def schedule(self, day: str, workout_id: int, name: str, seconds: int, tss: int,
                       now: datetime | None = None) -> datetime:
        """Puts the uploaded workout on MyWhoosh's calendar, so it's easy to find in the app: at 20:00 on its own day,
        or today for a past session being ridden late, and never in the past (sent after 20:00: in five minutes).
        The new entry is made first and the one this add-on made for that plan day before is removed after, so a
        failure never leaves the day without one. Returns the local start time. Raises MyWhooshError; the workout
        itself is uploaded either way."""
        now = (now or datetime.now()).replace(second=0, microsecond=0)
        tok = self.db.get("mywhoosh_token") or {}
        headers = {"Authorization": f"Bearer {tok.get('access_token', '')}"}
        tasks = self.db.get("mywhoosh_tasks") or {}
        at = max(datetime.combine(max(date.fromisoformat(day), now.date()), CALENDAR_AT), now + timedelta(minutes=5))
        start = int(at.astimezone().timestamp())  # epoch seconds, the container's (HA's) time zone
        r = await self.http.post(f"{TASK_URL}/create", headers=headers, json={
            "TaskId": "", "TaskType": "E_Simple_Workout", "TaskStartedTimeEpoc": start, "TaskTypeId": str(workout_id),
            "CurDayId": "", "TaskState": "E_NotStarted", "DayNo": 0, "TaskEndEpochTime": start + seconds, "MapId": 0,
            "TaskName": name[:60], "TaskDescription": "Training Coach", "TotalKilometers": 0, "TotalElevation": 0,
            "TSS": tss or 0, "SportMode": "E_Cycling"})
        data = _body(r)
        inner = data.get("data") if isinstance(data.get("data"), dict) else {}
        # The reply's shape is from the community client; accept the id where a variant would put it, and log the
        # whole reply when it isn't found (the entry may exist anyway: then a re-send can't replace it)
        task_id = next((src[k] for src in (inner, data) for k in ("TaskId", "taskId", "Id", "id") if src.get(k)), None)
        if r.status_code >= 400 or data.get("status") is False or not task_id:
            log.warning("MyWhoosh calendar entry failed: %s %s", r.status_code, r.text[:300])
            raise MyWhooshError("it isn't on the MyWhoosh calendar")
        self.db.set("mywhoosh_tasks", tasks | {day: str(task_id)})
        if (old := tasks.get(day)) and old != str(task_id):
            try:
                d = await self.http.delete(f"{TASK_URL}/{old}", headers=headers)
                if d.status_code >= 400:  # a stale entry is only clutter, but say so: re-sends would stack them
                    log.warning("MyWhoosh calendar: removing the old entry %s for %s answered %s %s",
                                old, day, d.status_code, d.text[:200])
            except httpx.HTTPError as e:
                log.warning("MyWhoosh calendar: removing the old entry for %s failed: %s", day, e)
        return at


def _body(r: httpx.Response) -> dict:
    """The reply as a JSON object, or {} for anything else (a list, true, a page)."""
    try:
        data = r.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def jwt_exp(token: str) -> float | None:
    try:
        payload = token.split(".")[1]
        return float(json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["exp"])
    except (IndexError, ValueError, KeyError, TypeError):
        return None


def workout_id(day: str, whoosh_id: str = "") -> int:
    """The same Id for the same account and day, so a re-send updates. The account is in it too: whether MyWhoosh keys
    workouts per account or globally isn't known, and two athletes of this add-on mustn't overwrite each other.
    Below 2^53, so it survives JSON number handling."""
    account = zlib.crc32(whoosh_id.encode()) % 10**5
    return ID_BASE + account * 10**8 + int(date.fromisoformat(day).strftime("%Y%m%d"))


def _step(n: int, kind: str, secs: int, power: float = 0, start: float = 0, end: float = 0, rpm: int = 0,
          interval: int = 0) -> dict:
    return {"Id": n, "Pace": 1, "IntervalId": interval, "WorkoutMessage": [], "Rpm": rpm or 0, "StepType": kind,
            "Power": round(power, 4), "StartPower": round(start, 4), "EndPower": round(end, 4), "Time": int(secs),
            "IsManualGrade": False, "ManualGradeValue": 0, "ShowAveragePower": False, "FlatRoad": 0}


def to_steps(steps: list[dict], cheers: tuple[str, ...] = (), last_rep: str = "") -> list[dict]:
    """The coach's steps as MyWhoosh steps, with the same on-screen messages the .zwo gets (cues and rep cheers)."""
    out: list[dict] = []
    group = 0
    for s in steps:
        t, rpm = s["type"], s.get("cadence") or 0
        first = len(out)
        if t == "intervals":
            group += 1
            for _ in range(s["repeat"]):
                out.append(_step(len(out) + 1, "E_Normal", s["on_s"], s["on_power"], rpm=rpm, interval=group))
                if s["off_s"]:
                    out.append(_step(len(out) + 1, "E_Normal", s["off_s"], s["off_power"], rpm=rpm, interval=group))
        elif t == "freeride":
            out.append(_step(len(out) + 1, "E_FreeRide", s["duration_s"]))
        elif t in ("warmup", "cooldown") or abs(s["power_low"] - s["power_high"]) > 0.005:
            # A ramp: MyWhoosh ramps from StartPower to EndPower (for our cooldown, power_low is where it starts)
            kind = "E_CoolDown" if t == "cooldown" or s["power_high"] < s["power_low"] else "E_WarmUp"
            out.append(_step(len(out) + 1, kind, s["duration_s"], start=s["power_low"], end=s["power_high"], rpm=rpm))
        else:
            out.append(_step(len(out) + 1, "E_Normal", s["duration_s"], s["power_low"], rpm=rpm))
        # Messages are timed from the start of the coach's step; MyWhoosh times them within each of its steps
        block = out[first:]
        for offset, text in workouts.text_events(s, cheers, last_rep):
            at = 0
            for st in block:
                if offset < at + st["Time"] or st is block[-1]:
                    st["WorkoutMessage"].append({"Id": len(st["WorkoutMessage"]) + 1, "Time": max(0, offset - at),
                                                 "Message": text[:120]})
                    break
                at += st["Time"]
    return out


def to_workout(day: str, plan_day: dict, name: str, cheers: tuple[str, ...] = (), last_rep: str = "",
               whoosh_id: str = "") -> dict:
    steps = to_steps(plan_day["steps"], cheers, last_rep)
    is_test = plan_day.get("test_protocol", "none") != "none"
    return {
        "Id": workout_id(day, whoosh_id), "Name": name[:60], "Description": (plan_day.get("description") or "")[:500],
        "Mode": "E_Ride", "SportsModeType": 0, "ERGMode": TEST_ERG if is_test else "E_ON",
        "IsRecovery": plan_day.get("intensity") == "recovery", "IsIntervals": any(s["IntervalId"] for s in steps),
        "FTPMode": "E_NoFTP", "IsTT": False, "IsTSS": False, "IsIF": False, "FTPMultiplier": 0, "StressPoint": 0,
        "Time": sum(s["Time"] for s in steps), "CustomTagDescription": "", "CategoryId": 1, "SubcategoryId": 0,
        # A favourite is easier to find among MyWhoosh's many workouts (whether MyWhoosh honours it on upload is unknown)
        "Type": "E_Custom", "DisplayType": "E_byWatts", "StepCount": len(steps), "IsFavorite": True,
        "CompletedCount": 0, "WorkoutStepsArray": steps, "WorkoutSteps": {str(s["Id"]): s for s in steps},
        "WorkoutstepsTMap": [], "WokoutAssociationId": 0, "AuthorName": "Training Coach",
        "TSS": plan_day.get("target_tss") or 0, "IF": 0, "KJ": 0, "IsVODAvailable": False,
    }
