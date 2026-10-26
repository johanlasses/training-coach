"""FastAPI app served through Home Assistant ingress."""
from __future__ import annotations

import asyncio
import logging
import re
import time
import unicodedata
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from html import escape as html_escape
from pathlib import Path

import httpx
import markdown as md
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import analysis, chat, checkin, coaches, feedback, ftptest, ha, health, memory, overview, sports, stats, workouts
from . import coach as coach_mod
from . import llm
from .config import (ACTIVITY_SOURCES, DATA_DIR, FIELD_TYPES, HEALTH_SOURCES, LEVELS, SECRETS, SYNC_INTERVALS, WEEKDAYS,
                     WORKOUT_APPS, Config, level_for_wkg, load_config, save_config)
from .db import DB
from .garmin import GarminClient, GarminError
from .mywhoosh import MyWhooshClient, MyWhooshError
from .strava import REDIRECT_URI, RateLimited, StravaClient
from .sync import recompute, sync
from .zwift import ZwiftClient, ZwiftError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("training_coach")

HERE = Path(__file__).parent
DUE_WINDOW = timedelta(hours=3)
HEALTH_EVERY = timedelta(hours=1)  # a local HA read, no external API
GARMIN_HEALTH_EVERY = timedelta(hours=3)  # four Garmin calls; daily values that land once the watch syncs
SYNC_RETRY = timedelta(minutes=10)  # a failed sync before the check-in: don't sync again on every page, check in anyway
INGRESS_IP = "172.30.32.2"  # Supervisor on the hassio network: ingress requests come from here
ATHLETE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")  # HA user ids are uuid hex; also a folder name, so nothing else
STRAVA_APP = ("strava_client_id", "strava_client_secret")


class State:
    """One athlete: their DB, settings, Strava and Zwift logins, jobs and scheduler. The owner's State has no `owner`;
    the others take the Claude key (always) and the Strava API app (unless they set their own) from it."""

    def __init__(self, path: Path | None = None, owner: State | None = None, user_id: str = "", name: str = "") -> None:
        path = path or DATA_DIR / "training.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = DB(path)
        self.owner, self.user_id, self.name = owner, user_id, name
        self.jobs: dict[str, dict] = {}
        self.send_lock = asyncio.Lock()  # one Send to MyWhoosh at a time per athlete
        self.flash: tuple[str, str] | None = None   # (kind, message) shown once on the next page
        self._tasks: set[asyncio.Task] = set()
        self.sync_tried: datetime | None = None  # last scheduled attempt, so a failing sync doesn't retry every minute
        self._ha_cache: dict[str, tuple[float, object]] = {}
        self.reload()

    @property
    def is_owner(self) -> bool:
        return self.owner is None

    def reload(self) -> None:
        self.cfg = cfg = load_config(self.db)
        self.shared: set[str] = set()  # fields filled in from the owner, never saved in this DB
        if self.owner:
            own = self.owner.cfg
            cfg.anthropic_api_key = own.anthropic_api_key
            self.shared.add("anthropic_api_key")
            if not (cfg.strava_client_id or cfg.strava_client_secret):
                cfg.strava_client_id, cfg.strava_client_secret = own.strava_client_id, own.strava_client_secret
                self.shared.update(STRAVA_APP)
            # The add-on's Supervisor token can call any service and read any entity: an athlete only gets their
            # phone's notifications and sensors (checked against the phone on save, this is the backstop).
            if not cfg.notify_service.startswith("notify.mobile_app_"):
                cfg.notify_service = ""
            for m in health.METRICS:
                if not getattr(cfg, f"health_{m}_entity").startswith("sensor."):
                    setattr(cfg, f"health_{m}_entity", "")
        self.strava = StravaClient(self.db, cfg.strava_client_id, cfg.strava_client_secret)
        self.garmin = GarminClient(self.db, cfg.garmin_api_key)
        self.zwift = ZwiftClient(self.db)
        self.mywhoosh = MyWhooshClient(self.db)

    def connected(self) -> dict[str, bool]:
        """Which third-party connections work, by the keys Profile's choices use: a choice only becomes selectable
        once it's connected."""
        return {"strava": self.strava.configured, "garmin": self.garmin.configured, "apple": bool(health.entities(self.cfg)),
                "zwift": self.zwift.configured, "mywhoosh": self.mywhoosh.configured}

    @property
    def workout_app(self) -> ZwiftClient | MyWhooshClient:
        """Where "Send to …" sends indoor sessions (Profile → Connections)."""
        return self.mywhoosh if self.cfg.workout_app == "mywhoosh" else self.zwift

    @property
    def source(self) -> StravaClient | GarminClient:
        """Where rides come from (Profile → Connections)."""
        return self.garmin if self.cfg.activity_source == "garmin" else self.strava

    @property
    def source_name(self) -> str:
        return ACTIVITY_SOURCES[self.cfg.activity_source]

    def own_cfg(self) -> Config:
        """The settings this athlete set themselves: the owner's shared values blanked out."""
        return replace(self.cfg, **{k: "" for k in self.shared})

    def save(self) -> None:
        save_config(self.db, self.own_cfg())

    async def phones(self) -> list[str] | None:
        """Slugs of the athlete's own phones (HA companion app); None for the owner, who can pick any entity."""
        if self.is_owner:
            return None
        async def read():
            return await ha.user_devices(self.user_id) or None  # an empty answer isn't cached: the app may be new
        return await self.ha_cached("phones", read) or []

    def start_job(self, name: str, coro) -> bool:
        """Run a coroutine in the background, at most one per name. Returns False if already running."""
        if self.jobs.get(name, {}).get("running"):
            coro.close()
            return False
        self.jobs[name] = {"running": True, "error": None, "started": datetime.now().isoformat(timespec="seconds")}

        async def wrapper():
            try:
                await coro
                self.jobs[name].update(running=False, finished=datetime.now().isoformat(timespec="seconds"))
            except Exception as e:  # noqa: BLE001 - surface any failure in the UI
                log.exception("Job %s failed", name)
                self.jobs[name].update(running=False, error=str(e))

        task = asyncio.create_task(wrapper())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    # --- jobs ---------------------------------------------------------------
    async def do_sync(self) -> None:
        if not self.source.configured:
            raise RuntimeError(f"{self.source_name} is not connected — see Profile")
        try:
            await sync(self.db, self.source, self.cfg)
        finally:
            if getattr(self, "checkin_after_sync", False):  # waiting for this sync; with stale data if it failed
                self.checkin_after_sync = False
                self.maybe_checkin(synced=True)
        await self.publish()
        # New rides get the coach's feedback and a notification. Its own job, so the sync shows done right away.
        if self.db.activities_to_notify((datetime.now(timezone.utc) - feedback.WINDOW).strftime("%Y-%m-%dT%H:%M:%SZ")):
            self.feedback_pending = True  # a running feedback job picks the new rides up before it ends
            self.start_job("feedback", self.do_feedback())

    async def do_feedback(self) -> None:
        handled = 0
        while getattr(self, "feedback_pending", False):
            self.feedback_pending = False
            while n := await feedback.after_sync(self.db, self.cfg):  # more than PER_RUN new rides: keep going
                handled += n
        if handled:
            await self.publish()

    async def do_ride_feedback(self, activity_id: int) -> None:
        a = self.db.activity(activity_id)
        if a:
            await feedback.generate(self.db, self.cfg, a)
            await self.publish()

    def snapshot(self) -> dict:
        today = date.today()
        snap = stats.snapshot(self.db.activities(), today, self.cfg)
        snap["ftp_tests"] = self.recent_tests(today)
        snap["coach_notes"] = [{k: n[k] for k in ("kind", "start", "end", "text")}
                               for n in self.db.active_notes(today.isoformat())]
        if health.enabled(self.cfg):
            snap["health"], snap["health_source"] = health.summary(self.db, today), HEALTH_SOURCES[self.cfg.health_source]
        snap["athlete_history"] = memory.history(self.db, self.cfg, datetime.now())
        return snap

    def recent_tests(self, today: date, weeks: int = 12) -> list[dict]:
        """FTP tests planned in the last weeks' active plans, whether they were ridden, and the FTP set from them."""
        acts = self.db.activities(since_local=(stats.week_start(today) - timedelta(weeks=weeks)).isoformat())
        applied = {t["date"]: t["ftp"] for t in self.cfg.ftp_tests}
        out = []
        for n in range(weeks, -1, -1):
            plan = self.db.active_plan((stats.week_start(today) - timedelta(weeks=n)).isoformat())
            for d in (plan or {}).get("payload", {}).get("days", []):
                if d.get("test_protocol", "none") == "none" or d["date"] >= today.isoformat():
                    continue
                # An easy ride swapped in for the test isn't a test: it needs a maximal effort
                done = any(ftptest.estimate(d["test_protocol"], a.get("mmp")) and stats.maximal_effort(a) for a in acts
                           if a["start_date_local"][:10] == d["date"])
                out.append({"date": d["date"], "protocol": d["test_protocol"], "done": done}
                           | ({"ftp_set_w": applied[d["date"]]} if d["date"] in applied else {}))
        return out

    async def ha_cached(self, key: str, read):
        """read()'s result, kept for 10 minutes so page loads don't hammer HA. None (HA unreachable) isn't kept."""
        hit = self._ha_cache.get(key)
        if hit and time.monotonic() - hit[0] < 600:
            return hit[1]
        value = await read()
        if value is not None:
            self._ha_cache[key] = (time.monotonic(), value)
        return value

    async def health_options(self) -> list[dict] | None:
        """Apple Health-looking sensors for Profile's pickers (an athlete's own phone only); None when HA can't be read."""
        phones = await self.phones()
        async def read():
            states = await ha.states()
            return health.candidates(states, phones) if states else None
        return await self.ha_cached("health", read)

    def maybe_checkin(self, synced: bool = False) -> None:
        """The coach's daily check-in, on the first page view of the day with something to talk about (so a new athlete
        gets one after their first sync). Rides must be in first, or last night's ride reads as missed: while a sync runs
        or the last one is stale, it syncs and the check-in follows the sync. Tried once a day: a failure doesn't retry
        on every page."""
        today = date.today()
        if not self.cfg.anthropic_api_key or self.db.get("checkin_tried") == today.isoformat():
            return
        utc = datetime.now(timezone.utc)
        if not synced and (self.jobs.get("sync") or {}).get("running"):
            self.checkin_after_sync = True
            return
        if not synced and self.source.configured and self.sync_stale(utc) and not (
                self.sync_tried and utc - self.sync_tried < SYNC_RETRY):
            self.sync_tried, self.checkin_after_sync = utc, True
            self.start_job("sync", self.do_sync())
            return
        if checkin.worth_it(self.db, self.db.activities(since_local=(today - timedelta(days=checkin.LOOKBACK_DAYS)).isoformat()), today):
            self.db.set("checkin_tried", today.isoformat())
            self.start_job("checkin", checkin.generate(self.db, self.cfg, today))

    def sync_stale(self, utc: datetime) -> bool:
        """The last sync is older than the sync interval (an hour when scheduled syncs are off)."""
        last = self.db.get("last_sync")
        return not last or utc - datetime.fromisoformat(last["at"]) > timedelta(minutes=self.cfg.sync_interval_min or 60)

    def checkin_unread(self) -> bool:
        last = self.db.last_chat_id("checkin")
        return bool(last) and last > (self.db.get("checkin_seen") or 0)

    async def do_chat(self) -> None:
        await chat.respond(self.db, self.cfg, self.snapshot(), date.today())

    async def do_plan(self, week_start: date, coach_key: str | None = None, activate: bool = False) -> None:
        """`activate`: the new plan runs the week (the Plan page's button: it shows one plan, the athlete's coach's,
        so the plan they just made must be the one Overview, notifications and the sensor follow)."""
        plan_id = await coach_mod.generate_plan(self.db, self.cfg, self.snapshot(), week_start, date.today(), coach_key)
        if activate and plan_id:
            self.db.set_active_plan(week_start.isoformat(), plan_id)
        await self.publish()

    async def do_analysis(self, coach_key: str) -> None:
        await analysis.run_analysis(self.db, self.cfg, self.snapshot(), "manual", coach_key)

    async def publish(self) -> None:
        if not self.is_owner:  # the sensors are the owner's; other athletes' would only clutter the owner's HA
            return
        today = date.today()
        d = stats.dashboard(self.db.activities(since_local=(today - timedelta(days=400)).isoformat()), today, self.cfg)
        session = overview.today_session(self.db, today)
        last = next(iter(self.db.activities(limit=1)), None)
        await ha.publish_sensors({
            "ftp": d["ftp"], "wkg": d["wkg"], "ctl": d["ctl"], "atl": d["atl"], "tsb": d["tsb"],
            "week_hours": d["this_week"]["hours"], "week_km": d["this_week"]["km"],
            "today_session": session["title"] if session else "Rest",
            "last_ride": ((last.get("feedback") or {}).get("headline") or last["name"])[:250] if last else "",
        })

    async def do_auto_plan(self, week_start: date) -> None:
        try:
            await self.do_plan(week_start)
        except Exception as e:
            await ha.notify(self.cfg.notify_service, "Next week's plan failed", str(e)[:180])
            raise
        await ha.notify(self.cfg.notify_service, "Next week's plan is ready",
                        f"{plan_coach_names(self.cfg, week_start)} planned the week of {week_start.strftime('%-d %b')}.",
                        ha.tap(f"/plan/week/{week_start.isoformat()}", "plan"))

    async def do_auto_analysis(self) -> None:
        try:
            await analysis.run_analysis(self.db, self.cfg, self.snapshot(), "scheduled")
        except Exception as e:
            await ha.notify(self.cfg.notify_service, "Weekly analysis failed", str(e)[:180])
            raise
        await ha.notify(self.cfg.notify_service, "Your weekly analysis is ready", "Tap to read it.",
                        ha.tap("/analysis", "analysis"))

    def due(self, key: str, now: datetime, at: str, weekday: str | None = None) -> bool:
        """True in the 3 hours after `at` (on `weekday`, if given) until the slot is marked as run with `ran`.

        The window keeps a restart in the afternoon from firing the morning's jobs, and switching a job on
        long after its time doesn't spend tokens right away."""
        if not at or (weekday and WEEKDAYS[now.weekday()] != weekday) or self.db.get(key) == now.date().isoformat():
            return False
        start = datetime.combine(now.date(), datetime.strptime(at, "%H:%M").time())
        return start <= now.replace(tzinfo=None) < start + DUE_WINDOW

    def ran(self, key: str, now: datetime) -> None:
        self.db.set(key, now.date().isoformat())

    async def scheduler(self, delay: float = 0) -> None:
        """Minute tick. Every job, and when it runs, is set on Settings → Schedules. Claude jobs are off by default.
        `delay` staggers the athletes, so their Strava syncs don't land in the same second."""
        if delay:
            await asyncio.sleep(delay)
        while True:
            try:
                now = datetime.now()
                cfg = self.cfg
                last, utc = self.db.get("last_sync"), datetime.now(timezone.utc)
                every = timedelta(minutes=cfg.sync_interval_min)
                if self.source.configured and cfg.sync_interval_min and (
                        not last or utc - datetime.fromisoformat(last["at"]) > every) and (
                        not self.sync_tried or utc - self.sync_tried > every):
                    self.sync_tried = utc
                    self.start_job("sync", self.do_sync())
                if (cfg.zwift_refresh and cfg.workout_app == "zwift" and self.zwift.configured
                        and self.db.get("zwift_refreshed") != now.date().isoformat()):
                    # Keycloak drops idle sessions; a daily refresh keeps the Zwift login alive.
                    self.db.set("zwift_refreshed", now.date().isoformat())
                    try:
                        await self.zwift.refresh()
                    except ZwiftError as e:
                        log.warning("Zwift token refresh: %s", e)
                fetched = self.db.get("health_fetched")
                garmin_health = cfg.health_source == "garmin" and self.garmin.configured
                if (garmin_health or (cfg.health_source == "apple" and health.entities(cfg) and ha.available())) and (
                        not fetched or now - datetime.fromisoformat(fetched) >= (GARMIN_HEALTH_EVERY if garmin_health else HEALTH_EVERY)):
                    self.db.set("health_fetched", now.isoformat(timespec="seconds"))
                    self.start_job("health", health.fetch_garmin(self.db, self.garmin, now.date()) if garmin_health
                                   else health.fetch(self.db, cfg, now.date()))
                if cfg.notify_service and self.due("notified", now, cfg.notify_time):
                    self.ran("notified", now)
                    s = overview.today_session(self.db, now.date())
                    if s:
                        await ha.notify(cfg.notify_service, f"Today: {s['title']}",
                                        f"{s['duration_min']} min · {s['description'][:180]}", ha.tap("/plan", "today"))
                # A busy job (e.g. a plan you started by hand) leaves the slot open, so the next tick tries again.
                if cfg.anthropic_api_key and cfg.auto_plan and self.due("auto_plan_ran", now, cfg.auto_plan_time, cfg.auto_plan_day):
                    next_week = stats.week_start(now.date()) + timedelta(days=7)
                    if self.db.latest_plan(next_week.isoformat()) or self.start_job("plan", self.do_auto_plan(next_week)):
                        self.ran("auto_plan_ran", now)
                if cfg.anthropic_api_key and cfg.auto_analysis and self.due(
                        "auto_analysis_ran", now, cfg.auto_analysis_time, cfg.auto_analysis_day):
                    if self.start_job("analysis", self.do_auto_analysis()):
                        self.ran("auto_analysis_ran", now)
            except Exception:  # noqa: BLE001 - keep the scheduler alive
                log.exception("Scheduler tick failed")
            await asyncio.sleep(60)


def next_run(now: datetime, at: str, weekday: str | None = None) -> datetime | None:
    """The next time a daily (or weekly) slot opens, for the Settings page."""
    if not at:
        return None
    t = datetime.combine(now.date(), datetime.strptime(at, "%H:%M").time())
    for i in range(8):
        cand = t + timedelta(days=i)
        if cand > now and (not weekday or WEEKDAYS[cand.weekday()] == weekday):
            return cand
    return None


def plan_coach_names(cfg: Config, week_start: date) -> str:
    """"Next Level" or "Next Level and Next Level Run": the coaches who plan that week, primary first."""
    days = [week_start + timedelta(days=i) for i in range(7)]
    active = list(dict.fromkeys(s for d in days for s in cfg.active_sports(d)))
    return " and ".join(coaches.get(cfg.coach_key(s), s).name for s in active)


def planned_by_activity(db: DB, acts: list[dict]) -> dict[int, dict]:
    """Activity id → the active plan's session that day it can be (a ride's ride, a run's run), for the feed."""
    out: dict[int, dict] = {}
    plans = {ws: db.active_plan(ws) for ws in {stats.week_start(date.fromisoformat(a["start_date_local"][:10])).isoformat()
                                                for a in acts}}
    for a in acts:
        days = (plans[stats.week_start(date.fromisoformat(a["start_date_local"][:10])).isoformat()] or {}).get("payload", {})
        if d := next((d for d in days.get("days", []) if d["date"] == a["start_date_local"][:10] and d["kind"] != "rest"
                      and coach_mod.counts_for(d, a)), None):
            out[a["id"]] = d
    return out


class NoAccess(Exception):
    def __init__(self, name: str) -> None:
        self.name = name


class Athletes:
    """Every athlete's State by Home Assistant user id. HA's owner (the user who set HA up) owns the add-on: they keep
    /data/training.db and the shared settings (Claude key, Strava app) and let others in. The others get their own
    DB under /data/athletes/<user id>/. Removing an athlete keeps their folder, so adding them back restores it."""

    def __init__(self) -> None:
        self.owner = State(DATA_DIR / "training.db")
        self.owner.name = self.owner.db.get("owner_name") or ""  # shown to the others ("Johan's Strava app")
        self.others = {uid: self._state(uid, a["name"]) for uid, a in self.listed().items()}
        self._tasks: dict[str, asyncio.Task] = {}
        self._parked: dict[str, State] = {}  # removed athletes' States, reused if they're approved again

    def _state(self, uid: str, name: str) -> State:
        return State(DATA_DIR / "athletes" / uid / "training.db", owner=self.owner, user_id=uid, name=name)

    def listed(self) -> dict[str, dict]:
        return self.owner.db.get("athletes") or {}

    def requests(self) -> dict[str, dict]:
        return self.owner.db.get("access_requests") or {}

    async def find_owner(self) -> bool:
        """Learns HA's owner once. Never the first visitor: with the panel open to every HA user, whoever opened it
        first after an update would take over the owner's data."""
        if self.owner.db.get("owner_user_id"):
            return True
        uid = await ha.owner_user_id()
        if uid:
            self.owner.db.set("owner_user_id", uid)
            log.info("Home Assistant's owner (%s) owns the add-on", uid)
        return bool(uid)

    async def keep_finding_owner(self) -> None:
        """At boot the add-on can start before HA Core does."""
        while not await self.find_owner():
            await asyncio.sleep(30)

    def resolve(self, uid: str, name: str) -> State:
        """The user's State; an unknown user is noted as asking for access."""
        owner_id = self.owner.db.get("owner_user_id")
        if not owner_id:
            raise HTTPException(503, "Training Coach is still asking Home Assistant who its owner is. Reload in a minute.")
        if uid == owner_id:
            if name != self.owner.name:
                self.owner.db.set("owner_name", name)
            self.owner.user_id, self.owner.name = uid, name
            return self.owner
        if uid in self.others:
            return self.others[uid]
        reqs = self.requests()
        if uid not in reqs:
            reqs[uid] = {"name": name, "at": datetime.now().isoformat(timespec="minutes")}
            self.owner.db.set("access_requests", reqs)
        raise NoAccess(name)

    def approve(self, uid: str) -> str | None:
        reqs = self.requests()
        if uid not in reqs:
            return None
        req = reqs.pop(uid)
        self.owner.db.set("access_requests", reqs)
        self.owner.db.set("athletes", self.listed() | {uid: {"name": req["name"], "added": date.today().isoformat()}})
        # Removed earlier in this run: the same State again, never a second one (a second SQLite connection to the
        # same file, and jobs the reset's busy check couldn't see)
        st = self._parked.pop(uid, None) or self._state(uid, req["name"])
        st.name = req["name"]
        self.others[uid] = st
        self.start(uid, st)
        return req["name"]

    def dismiss(self, uid: str) -> str | None:
        reqs = self.requests()
        req = reqs.pop(uid, None)
        self.owner.db.set("access_requests", reqs)
        return req and req["name"]

    def remove(self, uid: str) -> str | None:
        listed = self.listed()
        a = listed.pop(uid, None)
        self.owner.db.set("athletes", listed)
        if task := self._tasks.pop(uid, None):
            task.cancel()
        if st := self.others.pop(uid, None):
            for job in list(st._tasks):  # a sync or a coach call mustn't keep writing for someone who's gone
                job.cancel()
            st.jobs.clear()
            self._parked[uid] = st
        return a and a["name"]

    RESET_BACKUP_DAYS = 30  # pre-reset backups are kept this long; by age, so a run of resets can't push one out

    def reset(self, uid: str, confirm: str, activities: bool, health_data: bool, strava: bool) -> tuple[bool, str]:
        """The owner clears what went wrong in one athlete's add-on (e.g. the owner's own Strava signed in there):
        their synced activities, health data and/or Strava connection. Plans, chat, notes and memory are kept, as on
        a source switch. Returns (done, message).

        Every athlete has their own SQLite file, and this must only ever touch that one. So, before anything changes:
        the athlete is a listed non-owner whose State's DB file is exactly athletes/<uid>/training.db (never the
        owner's file), the owner typed that athlete's name, and no job is writing to the DB. Then the file is backed
        up next to itself, and only then cleared."""
        st, listed = self.others.get(uid), self.listed().get(uid)
        if not ATHLETE_ID.fullmatch(uid) or not st or not listed or st is self.owner:
            return False, "That athlete is gone."
        name = listed["name"]
        expected = (DATA_DIR / "athletes" / uid / "training.db").resolve()
        if st.db.path.resolve() != expected or expected == self.owner.db.path.resolve() or st.owner is not self.owner:
            log.error("Reset refused: %s's DB is %s, expected %s", uid, st.db.path, expected)
            return False, f"Refused: {name}'s data isn't where it should be. Nothing was changed."
        if not (activities or health_data or strava):
            return False, f"Nothing picked to reset for {name}."
        if same_name(confirm) != same_name(name):
            return False, f"Type {name}'s name to confirm the reset. Nothing was changed."
        if any(j.get("running") for j in st.jobs.values()):  # any job: a list of names would drift
            return False, f"{name}'s add-on is busy (a sync or the coach) right now. Try again in a minute."
        strava_id = (st.db.get("strava_athlete") or {}).get("id") if st.db.get("strava_token") else None
        if activities and not strava and (holder := self.strava_holder(strava_id, st)):
            # The next sync would bring the same wrong activities straight back
            return False, (f"{name}'s Strava is {holder.name or 'the owner'}'s account: tick Strava connection too, or "
                           "the next sync brings the same activities back. Nothing was changed.")
        left = {"activities": activities and st.db.count_activities() > 0,
                "health": health_data and st.db.count_health_days() > 0, "strava": strava and bool(strava_id)}
        if not any(left.values()):  # e.g. the same form sent twice: nothing to do, and no backup of an empty DB
            return False, f"Nothing left to reset for {name}: what you ticked is already empty."
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup = expected.with_name(f"training.db.before-reset-{stamp}")
        if backup.exists():
            return False, f"A reset for {name} is already under way. Nothing was changed."
        try:
            st.db.backup(backup)
        except Exception as e:  # noqa: BLE001 - no backup, no reset
            log.exception("Reset backup failed for %s", uid)
            backup.unlink(missing_ok=True)  # a half-written file isn't a backup
            return False, f"Couldn't back up {name}'s data first ({e}). Nothing was changed."
        cutoff = time.time() - self.RESET_BACKUP_DAYS * 86400
        for old in expected.parent.glob("training.db.before-reset-*"):
            if old != backup and old.stat().st_mtime < cutoff:
                old.unlink(missing_ok=True)
        log.info("Reset for %s (%s): activities=%s health=%s strava=%s; backup %s", name, uid, activities, health_data,
                 strava, backup.name)
        done = []
        if strava:
            st.strava.disconnect()
            done.append("Strava disconnected")
        if activities:
            st.db.clear_activities()
            for k in ("last_activity_start", "last_sync", "kinds_backfilled"):
                st.db.set(k, None)
            st.db.set("mark_notified", True)  # what syncs back in isn't new: no round of pushes
            done.append("synced activities removed")
        if health_data:
            st.db.clear_health()
            st.db.set("health_fetched", None)
            done.append("health data removed")
        return True, (f"{name}: {', '.join(done)}. Plans, chat, notes and memory are kept. A backup from before the "
                      f"reset is in athletes/{uid}/{backup.name}.")

    def strava_holder(self, strava_id, but: State) -> State | None:
        """Another athlete (or the owner) whose add-on already has this Strava account connected."""
        for st in [self.owner, *self.others.values()]:
            if st is not but and strava_id and (st.db.get("strava_athlete") or {}).get("id") == strava_id \
                    and st.db.get("strava_token"):
                return st
        return None

    def reload_shared(self) -> None:
        """After the owner's settings change: the others pick up the Claude key and Strava app."""
        for st in self.others.values():
            st.reload()

    def start(self, key: str, st: State) -> None:
        self._tasks[key] = asyncio.create_task(st.scheduler(delay=(len(self._tasks) * 7) % 60))

    def start_all(self) -> None:
        self._tasks["owner?"] = asyncio.create_task(self.keep_finding_owner())
        self.start("", self.owner)
        for uid, st in self.others.items():
            self.start(uid, st)

    def stop_all(self) -> None:
        for task in self._tasks.values():
            task.cancel()


def same_name(s: str) -> str:
    """A typed name as compared for the reset's confirmation: case, spacing, and iOS's curly apostrophes don't count."""
    s = unicodedata.normalize("NFKC", s).replace("’", "'").replace("‘", "'")
    return " ".join(s.split()).casefold()


def athlete(request: Request) -> State:
    """The State of the Home Assistant user behind the request. Supervisor's ingress sets X-Remote-User-Id and drops
    any the browser sent, so it's only trusted from Supervisor. Outside HA (local runs, tests) it's the owner's."""
    reg: Athletes = request.app.state.athletes
    if not ha.SUPERVISOR_TOKEN:
        return reg.owner
    uid = request.headers.get("X-Remote-User-Id", "")
    if not request.client or request.client.host != INGRESS_IP or not ATHLETE_ID.fullmatch(uid):
        raise HTTPException(403, "Open Training Coach from the Home Assistant sidebar")
    name = request.headers.get("X-Remote-User-Display-Name") or request.headers.get("X-Remote-User-Name") or "Someone"
    return reg.resolve(uid, name)


def owner_only(request: Request) -> State:
    st = athlete(request)
    if not st.is_owner:
        raise HTTPException(403, "Only the add-on's owner can do that")
    return st


@asynccontextmanager
async def lifespan(app: FastAPI):
    reg = Athletes()
    app.state.athletes = reg
    app.state.st = reg.owner  # for tests and debugging: routes always go through athlete(request)
    log.info("Home Assistant API: %s", "via Supervisor" if ha.SUPERVISOR_TOKEN else ha.HA_URL or
             "not available (no SUPERVISOR_TOKEN: notifications, sensors and Apple Health are off)")
    log.info("Athletes: owner + %d", len(reg.others))
    await reg.find_owner()  # before the first page; keeps trying in the background if HA isn't up yet
    reg.start_all()
    yield
    reg.stop_all()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")


@app.exception_handler(NoAccess)
async def no_access(request: Request, exc: NoAccess) -> HTMLResponse:
    return templates.TemplateResponse(request, "no_access.html", {"base": base(request), "name": exc.name}, status_code=403)


def fmt_duration(secs: int | None) -> str:
    secs = secs or 0
    h, m = divmod(round(secs / 60), 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def fmt_dur_label(secs: int) -> str:
    return f"{secs // 60} min" if secs >= 60 else f"{secs} s"


templates.env.filters["duration"] = fmt_duration
templates.env.filters["dur_label"] = fmt_dur_label
templates.env.filters["day_label"] = lambda iso: date.fromisoformat(iso).strftime("%a %-d %b")
templates.env.globals["workout_svg"] = workouts.svg_profile
templates.env.globals["coaches"] = coaches.COACHES
templates.env.globals["ride_profiles"] = workouts.ride_profiles
templates.env.filters["coach_name"] = coaches.name_of
templates.env.filters["coach_short"] = coaches.short_name_of
templates.env.filters["first_sentence"] = overview.first_sentence
templates.env.filters["pace"] = stats.pace
templates.env.filters["is_ride"] = sports.is_ride


def base(request: Request) -> str:
    """Ingress prefix (e.g. /api/hassio_ingress/<token>); empty when accessed directly."""
    return request.headers.get("X-Ingress-Path", "").rstrip("/")


def render(request: Request, name: str, **ctx) -> Response:
    st = athlete(request)
    st.maybe_checkin()
    flash, st.flash = st.flash, None
    return templates.TemplateResponse(request, name, {
        "base": base(request), "jobs": st.jobs, "last_sync": st.db.get("last_sync"), "flash": flash,
        "is_owner": st.is_owner, "owner_name": (st.owner.name if st.owner else st.name) or "the owner",
        "source_ok": st.source.configured, "source_name": st.source_name, "activity_source": st.cfg.activity_source,
        "strava_ok": st.strava.configured, "zwift_ok": st.zwift.configured, "ai_ok": bool(st.cfg.anthropic_api_key),
        "workout_app": st.cfg.workout_app, "workout_app_name": WORKOUT_APPS[st.cfg.workout_app],
        "workout_app_ok": st.workout_app.configured,
        "checkin_unread": st.checkin_unread(), **ctx})


def render_md(text: str) -> str:
    """Markdown from the model to HTML. Raw HTML is escaped first: the prompts carry user-controlled text (ride
    names, chat), so the output is never trusted as markup."""
    return md.markdown(html_escape(text, quote=False), extensions=["tables"])


def back(request: Request, path: str) -> RedirectResponse:
    return RedirectResponse(f"{base(request)}{path}", status_code=303)


@app.get("/")
async def home(request: Request):
    st = athlete(request)
    today = date.today()
    acts = st.db.activities(since_local=(today - timedelta(days=400)).isoformat())
    d = stats.dashboard(acts, today, st.cfg)
    feed = acts[:20]
    status = overview.status(st.db, st.cfg, acts, today, d)
    ready = health.readiness(health.summary(st.db, today)) if health.enabled(st.cfg) else None
    return render(request, "home.html", d=d, status=status, gauges=overview.gauges(status, ready, d["fitness"]),
                  latest=feed[0] if feed else None, feed=feed[1:], planned=planned_by_activity(st.db, feed),
                  feedback_running=(st.jobs.get("feedback") or {}).get("running"))


@app.get("/activity/{activity_id}")
async def activity(request: Request, activity_id: int):
    st = athlete(request)
    a = st.db.activity(activity_id)
    if not a:
        raise HTTPException(404)
    if a.get("duplicate_of") and st.db.activity(a["duplicate_of"]):  # a second recording (an old push link): the kept one
        return back(request, f"/activity/{a['duplicate_of']}")
    day = date.fromisoformat(a["start_date_local"][:10])
    acts = st.db.activities(since_local=(day - timedelta(days=95)).isoformat())
    from .metrics import ZONES, zone_seconds
    zones = list(zip([z for z, _ in ZONES], zone_seconds(a["power_hist"], a["ftp_used"]))) if a.get("power_hist") else []
    fb = a.get("feedback")
    return render(request, "activity.html", a=a, curve90=stats.power_curve(acts, day, 90), zones=zones,
                  planned=feedback.plan_day(st.db, day, a["name"] or "", a)[1],
                  fb_html=render_md(fb["detail"]) if fb else None, fb_coach=coaches.name_of(fb["coach"]) if fb else None)


@app.post("/activity/{activity_id}/feedback")
async def activity_feedback(request: Request, activity_id: int):
    st = athlete(request)
    if not st.db.activity(activity_id):
        raise HTTPException(404)
    if not st.start_job("ride_feedback", st.do_ride_feedback(activity_id)):
        st.flash = ("error", "The coach is still writing feedback on another ride. Try again in a moment.")
    return back(request, f"/activity/{activity_id}")


def week_of(week: str | None, today: date) -> date:
    """The Monday of the week a ?week= date falls in (plans are stored by their Monday: any other date would find no
    plan, and planning it would store one under the wrong key); this week without one, or for a date that isn't one."""
    try:
        return stats.week_start(date.fromisoformat(week)) if week else stats.week_start(today)
    except ValueError:
        return stats.week_start(today)


@app.get("/plan/week/{week}")
async def plan_week_view(request: Request, week: str):
    """A path-only link to a week, for notifications: ingress deep links may drop the query string."""
    return await plan_view(request, week=week)


@app.get("/plan")
async def plan_view(request: Request, week: str | None = None):
    """The week planned by the athlete's coach (Profile; with two sports, the primary sport's coach leads and the
    week shows both); a week that coach didn't plan shows its active plan, from whoever made it, so a past week is
    never empty."""
    st = athlete(request)
    today = date.today()
    ws = week_of(week, today)
    plans = {k or "": p for k, p in st.db.week_plans(ws.isoformat()).items()}
    active = st.db.active_plan(ws.isoformat())
    lead = st.cfg.active_sports(max(today, ws))[0]
    coach = coaches.get(st.cfg.coach_key(lead), lead).key
    plan = plans.get(coach) or active
    acts = st.db.activities(since_local=ws.isoformat())
    by_day: dict[str, list] = {}
    for a in acts:
        by_day.setdefault(a["start_date_local"][:10], []).append(a)
    status, tests, matched = {}, {}, {}
    if plan:
        for d in plan["payload"]["days"]:
            k = sports.day_key(d)
            day_acts = by_day.get(d["date"], [])
            matched[k] = [a for a in day_acts if d["kind"] == "rest" or coach_mod.counts_for(d, a)]
            status[k] = "upcoming" if d["date"] > today.isoformat() else coach_mod.compliance(d, day_acts)
            results = [r for a in day_acts if (r := ftptest.estimate(d.get("test_protocol", "none"), a.get("mmp")))]
            if results:
                tests[d["date"]] = max(results)
    return render(request, "plan.html", plan=plan, coach=coach, coach_names=plan_coach_names(st.cfg, ws),
                  matched=matched, two_sports=len({sports.day_sport(d) for d in (plan or {}).get("payload", {}).get("days", [])}) > 1,
                  active_coach=active["coach"] if active else None,
                  active_id=active["id"] if active else None, week=ws, prev=ws - timedelta(days=7),
                  next=ws + timedelta(days=7), today=today.isoformat(), status=status, by_day=by_day, tests=tests,
                  applied_tests={t["date"] for t in st.cfg.ftp_tests}, needs_profile=needs_profile(st),
                  uploads=st.db.get(f"{st.cfg.workout_app}_uploads") or {}, past=ws + timedelta(days=6) < today)


def zwift_day(st: State, plan_id: int, day: str) -> tuple[dict, dict, str]:
    """(plan, day, .zwo XML) for a planned Zwift session; 404 otherwise. Cheers come from the plan's coach."""
    plan = st.db.plan(plan_id)
    d = next((d for d in (plan or {}).get("payload", {}).get("days", []) if d["date"] == day and d["kind"] == "zwift"), None)
    if not d:
        raise HTTPException(404)
    c = coaches.get(d.get("coach") or plan["coach"])
    name = f"{date.fromisoformat(day).strftime('%a %-d %b')} · {d['title']}"
    return plan, d, workouts.to_zwo(name, d["description"], d["steps"], c.cheers, c.last_rep)


@app.post("/plan/{plan_id}/{day}/zwift")
async def zwift_upload(request: Request, plan_id: int, day: str):
    st = athlete(request)
    plan, d, xml = zwift_day(st, plan_id, day)
    try:
        await st.zwift.upload_workout(workouts.filename(day, d["title"]), xml)
    except (ZwiftError, httpx.HTTPError) as e:
        log.warning("Zwift upload failed: %s", e)
        st.flash = ("error", f"Couldn't send “{d['title']}” to Zwift: {e}")
    else:
        uploads = st.db.get("zwift_uploads") or {}
        uploads[f"{plan_id}/{day}"] = datetime.now().isoformat(timespec="minutes")
        st.db.set("zwift_uploads", uploads)
        st.flash = ("ok", f"“{d['title']}” is in your Zwift account. Restart Zwift, then find it under "
                          "Workouts → Custom Workouts → Training Coach.")
    return back(request, f"/plan?week={plan['week_start']}")


@app.post("/plan/{plan_id}/{day}/mywhoosh")
async def mywhoosh_upload(request: Request, plan_id: int, day: str):
    st = athlete(request)
    plan, d, _ = zwift_day(st, plan_id, day)  # the same indoor sessions, the same coach's cheers
    c = coaches.get(d.get("coach") or plan["coach"])
    name = f"{date.fromisoformat(day).strftime('%a %-d %b')} · {d['title']}"
    async with st.send_lock:  # a double tap mustn't make two calendar entries, one of them never tracked
        try:
            workout_id = await st.mywhoosh.upload_workout(day, d, name, c.cheers, c.last_rep)
        except (MyWhooshError, httpx.HTTPError) as e:
            log.warning("MyWhoosh upload failed: %s", e)
            st.flash = ("error", f"Couldn't send “{d['title']}” to MyWhoosh: {e}")
            return back(request, f"/plan?week={plan['week_start']}")
        uploads = st.db.get("mywhoosh_uploads") or {}
        uploads[f"{plan_id}/{day}"] = datetime.now().isoformat(timespec="minutes")
        st.db.set("mywhoosh_uploads", uploads)
        try:  # the calendar entry makes it easy to find among MyWhoosh's workouts; the upload counts either way
            at = await st.mywhoosh.schedule(day, workout_id, name, workouts.total_seconds(d["steps"]), d.get("target_tss") or 0)
            where = "today" if at.date() == date.today() else at.strftime("%a %-d %b")
            st.flash = ("ok", f"“{d['title']}” is in MyWhoosh: on your calendar {where} at {at:%H:%M} and in My Workouts. "
                              "Sending this day again replaces it.")
        except (MyWhooshError, httpx.HTTPError) as e:
            log.warning("MyWhoosh calendar entry failed: %s", e)
            st.flash = ("ok", f"“{d['title']}” is in MyWhoosh under My Workouts, but it isn't on the MyWhoosh calendar.")
    return back(request, f"/plan?week={plan['week_start']}")


@app.get("/analysis")
async def analysis_view(request: Request, id: int | None = None):
    st = athlete(request)
    a = st.db.analysis(id)
    html = render_md(a["markdown"]) if a else None
    return render(request, "analysis.html", a=a, html=html, history=st.db.analyses(),
                  analysis_coach=analysis_coach(st.cfg))


@app.post("/sync")
async def sync_now(request: Request):
    st = athlete(request)
    if st.source is st.strava and st.strava.paused_until > time.time():
        until = datetime.fromtimestamp(st.strava.paused_until, timezone.utc).strftime("%H:%M")
        st.flash = ("ok", f"Strava's rate limit was reached. Sync resumes on its own at {until} UTC.")
        return back(request, "/")
    st.start_job("sync", st.do_sync())
    return back(request, "/")


def needs_profile(st: State) -> bool:
    """A new athlete plans on defaults (days, hours, FTP) until they've saved their Profile once."""
    return not st.is_owner and not st.db.get("profile_saved")


@app.post("/plan")
async def plan_now(request: Request, week: str | None = None):
    st = athlete(request)
    ws = week_of(week, date.today())
    if needs_profile(st):
        st.flash = ("error", "Fill in your Profile first: the coach plans around your days, hours and goals.")
        return back(request, f"/plan?week={ws.isoformat()}")
    if ws + timedelta(days=6) < date.today():  # nothing left to plan; a stale page mustn't rewrite history
        st.flash = ("error", "That week is over: there's nothing left to plan.")
        return back(request, f"/plan?week={ws.isoformat()}")
    st.start_job("plan", st.do_plan(ws, activate=True))  # the athlete's coaches plan, primary sport first
    return back(request, f"/plan?week={ws.isoformat()}")


@app.post("/plan/{plan_id}/activate")
async def plan_activate(request: Request, plan_id: int):
    st = athlete(request)
    plan = st.db.plan(plan_id)
    if not plan:
        raise HTTPException(404)
    st.db.set_active_plan(plan["week_start"], plan_id)
    st.start_job("publish", st.publish())  # MQTT round-trip: don't hold the redirect
    return back(request, f"/plan?week={plan['week_start']}")


@app.get("/coach")
async def coach_view(request: Request):
    st = athlete(request)
    today = date.today()
    messages = st.db.chat(100)
    if last := st.db.last_chat_id("checkin"):
        st.db.set("checkin_seen", last)
    active = st.db.active_plan(stats.week_start(today).isoformat())
    for m in messages:  # old → new title per changed day, for the proposal diff
        if m["proposal"]:
            old_plan = st.db.plan(m["proposal"]["plan_id"])
            sport = m["proposal"].get("sport", "cycling")
            old = {d["date"]: d for d in chat.own_days(old_plan, [x["date"] for x in m["proposal"]["days"]], sport)} if old_plan else {}
            m["diff"] = [(d, old.get(d["date"])) for d in m["proposal"]["days"]]
            m["live"] = (not m["applied_plan_id"] and active and active["id"] == m["proposal"]["plan_id"]
                         and all(d["date"] >= today.isoformat() for d in m["proposal"]["days"]))
    team = chat.chat_coaches(st.db, st.cfg, today)
    picked = chat.addressed(st.db, st.cfg, today, request.query_params.get("to") or next(
        (m["coach"] for m in reversed(messages) if m["role"] == "user"), None))
    return render(request, "coach.html", messages=messages, coach=picked, team=team,
                  plan_coaches={c.sport: memory.plan_coach(active, c.sport) for c in team} if active else {},
                  notes=st.db.active_notes(today.isoformat()), has_plan=bool(active),
                  plan_coach=active["coach"] if active else None, today=today.isoformat(),
                  yesterday=(today - timedelta(days=1)).isoformat())


@app.get("/coach/status")
async def coach_status(request: Request):
    """Polled by the chat page while the coach is typing, so it reloads once when the reply is in."""
    jobs = athlete(request).jobs
    return JSONResponse({"running": any((jobs.get(k) or {}).get("running") for k in ("chat", "checkin"))})


@app.post("/coach")
async def coach_send(request: Request):
    st = athlete(request)
    form = await request.form()
    text = str(form.get("message", "")).strip()[:4000]
    to = chat.addressed(st.db, st.cfg, date.today(), str(form.get("coach", ""))).key  # the coach they tapped
    if (st.jobs.get("chat") or {}).get("running"):
        st.flash = ("error", "The coach is still answering your last message. Send this one when the reply is in.")
    elif text:
        st.db.add_chat(datetime.now().isoformat(timespec="seconds"), "user", to, text)
        st.start_job("chat", st.do_chat())
    return back(request, f"/coach?to={to}" if form.get("coach") else "/coach")  # the resolved key, never the raw field


@app.post("/coach/{msg_id}/apply")
async def coach_apply(request: Request, msg_id: int):
    st = athlete(request)
    msg = st.db.chat_message(msg_id)
    if not msg:
        raise HTTPException(404)
    plan_id, text = chat.apply(st.db, msg, date.today())
    st.flash = ("ok" if plan_id else "error", text)
    if plan_id:
        st.start_job("publish", st.publish())  # MQTT round-trip: don't hold the redirect
    return back(request, "/coach")


@app.post("/coach/clear")
async def coach_clear(request: Request):
    st = athlete(request)
    st.db.clear_chat()
    st.flash = ("ok", "Conversation cleared. Coach notes and memory are kept.")
    return back(request, "/coach")


@app.post("/notes/{note_id}/delete")
async def note_delete(request: Request, note_id: int):
    st = athlete(request)
    st.db.delete_note(note_id, datetime.now().isoformat(timespec="seconds"))
    return back(request, "/profile" if (await request.form()).get("from") == "profile" else "/coach")


@app.post("/memory/{memory_id}/delete")
async def memory_delete(request: Request, memory_id: int):
    st = athlete(request)
    st.db.forget_memory(memory_id, datetime.now().isoformat(timespec="seconds"))
    return back(request, "/profile")


@app.post("/ftp/apply")
async def ftp_apply(request: Request):
    """Records a field test's FTP from the test date on. Rides before it keep the FTP they had."""
    st = athlete(request)
    form = await request.form()
    try:
        ftp, day = int(str(form.get("ftp", ""))), date.fromisoformat(str(form.get("date", "")))
    except ValueError:
        ftp, day = 0, None
    if not day or not 80 <= ftp <= 600 or day > date.today():
        st.flash = ("error", "That doesn't look like a test result.")
    else:
        st.cfg.ftp_tests = [t for t in st.cfg.ftp_tests if t["date"] != day.isoformat()] + [
            {"date": day.isoformat(), "ftp": ftp, "protocol": str(form.get("protocol", ""))}]
        st.save()
        st.reload()
        recompute(st.db, st.cfg)
        st.start_job("publish", st.publish())  # MQTT round-trip: don't hold the redirect
        st.flash = ("ok", f"FTP {ftp} W from your test on {day.strftime('%-d %b')}. It counts from that day for 12 weeks, "
                          "then the estimate from your best efforts takes over." + (
                              " Your FTP override on Profile still wins until you clear it." if st.cfg.ftp_override else ""))
    return back(request, f"/plan?week={form.get('week', '')}")


@app.post("/ftp/tests/delete")
async def ftp_test_delete(request: Request, date_: str = Query(alias="date")):
    st = athlete(request)
    st.cfg.ftp_tests = [t for t in st.cfg.ftp_tests if t["date"] != date_]
    st.save()
    st.reload()
    recompute(st.db, st.cfg)
    st.start_job("publish", st.publish())  # MQTT round-trip: don't hold the redirect
    st.flash = ("ok", "Test result removed.")
    return back(request, "/profile")


def analysis_coach(cfg: Config) -> str:
    """The analysis is written by the primary active sport's coach (Profile)."""
    sport = cfg.active_sports(date.today())[0]
    return coaches.get(cfg.coach_key(sport), sport).key


@app.post("/analysis")
async def analysis_now(request: Request):
    st = athlete(request)
    st.start_job("analysis", st.do_analysis(analysis_coach(st.cfg)))  # the athlete's coach (Profile)
    return back(request, "/analysis")


DAY_NAMES = dict(zip(WEEKDAYS, ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]))
CHOICES = {"athlete_level": LEVELS, "cycling_coach": {c.key for c in coaches.for_sport("cycling")},
           "running_coach": {c.key for c in coaches.for_sport("running")}, "primary_sport": sports.SPORTS,
           "activity_source": ACTIVITY_SOURCES,
           "health_source": HEALTH_SOURCES, "workout_app": WORKOUT_APPS, "sync_interval_min": {str(k) for k in SYNC_INTERVALS},
           "auto_plan_day": DAY_NAMES, "auto_analysis_day": DAY_NAMES}
TIMES = {"notify_time", "auto_plan_time", "auto_analysis_time"}
DATES = {"cycling_until", "running_until"}
SECTION_BOOLS = {"schedules": {"zwift_refresh", "auto_plan", "auto_analysis"},  # unticked checkboxes are absent
                 "sports": {"cycling_active", "running_active", "run_in_dark"}}
SOURCE_FIELDS = {"activity_source", "health_source", "garmin_api_key"}  # their own routes, never apply_form
CONNECTION_NAMES = {"strava": "Strava", "garmin": "Garmin", "apple": "Apple Health", "zwift": "Zwift", "mywhoosh": "MyWhoosh"}


def valid_date(raw: str) -> bool:
    try:
        date.fromisoformat(raw)
        return True
    except ValueError:
        return False


def parse_pace(raw: str) -> int | None:
    """"4:45" (min:s per km; "4.45" and "4,45" too, the iPhone's keypads) → 285 s; empty → 0 (estimated); None if it
    isn't a sensible pace."""
    if not raw:
        return 0
    m = re.fullmatch(r"(\d{1,2})[:.,]([0-5]\d)(?:\s*/\s*km)?", raw)
    secs = int(m[1]) * 60 + int(m[2]) if m else None
    return secs if secs and 150 <= secs <= 720 else None  # 2:30–12:00 /km


def valid_time(raw: str) -> bool:
    try:
        datetime.strptime(raw, "%H:%M")
        return True
    except ValueError:
        return False


def own_phone(name: str, raw: str, phones: list[str]) -> bool:
    """An athlete's notify service and Apple Health sensors must be their own phone's."""
    if name == "notify_service":
        return raw in {f"notify.mobile_app_{p}" for p in phones}
    return not name.startswith("health_") or raw in health.phone_sensors(phones)  # exact: iphone is a prefix of iphone_2


def apply_form(st: State, form, phones: list[str] | None = None) -> list[str]:
    """Apply the config fields present in a submitted form and persist. Returns parse errors.

    Fields missing from the form are untouched, so each page only changes its own settings. `phones` is an
    athlete's own devices (see State.phones); None for the owner.
    """
    cfg = st.cfg
    errors = []
    before = memory.tracked(cfg)
    before_active = (cfg.cycling_active, cfg.running_active)
    skip: set[str] = set()
    if st.shared & set(STRAVA_APP) and any(str(form.get(k, "")).strip() for k in STRAVA_APP):
        # Leaving the shared Strava app takes both fields: half of it would pair with the owner's other half (and save it)
        if all(str(form.get(k, "")).strip() for k in STRAVA_APP):
            st.shared.difference_update(STRAVA_APP)
        else:
            errors.append("Strava app: enter both the client ID and the secret of your own app")
            skip.update(STRAVA_APP)
    for name, typ in FIELD_TYPES.items():
        if name == "available_days":
            if "availability" in form:  # only present when that section was submitted
                cfg.available_days = [d for d in WEEKDAYS if d in form.getlist("available_days")]
            continue
        if typ == "bool":
            if name in SECTION_BOOLS.get(str(form.get("_section", "")), ()):  # unticked checkboxes are absent
                setattr(cfg, name, name in form)
            continue
        if name not in form or name in skip or (name == "anthropic_api_key" and not st.is_owner) or name in SOURCE_FIELDS:
            continue  # sources only change through /profile/source, which clears what the old source brought
        raw = str(form[name]).strip()
        if phones is not None and raw and not own_phone(name, raw, phones):
            errors.append(f"{name.replace('_', ' ')}: {raw} isn't from your phone")
            continue
        if name in TIMES and raw and not valid_time(raw):
            errors.append(f"{name.replace('_', ' ')}: '{raw}' is not a time like 07:00")
            continue
        if name in DATES and raw and not valid_date(raw):
            errors.append(f"{name.replace('_', ' ')}: '{raw}' is not a date")
            continue
        if name == "threshold_pace":
            secs = parse_pace(raw)
            if secs is None:
                errors.append(f"threshold pace: '{raw}' is not a pace like 4:45")
            else:
                cfg.threshold_pace = secs
            continue
        if name == "run_lthr" and raw and not (raw.isdigit() and 100 <= int(raw) <= 220):
            errors.append(f"running threshold HR: '{raw}' isn't a heart rate between 100 and 220")
            continue
        if name in SECRETS and not raw:
            continue  # blank secret field = keep the stored value
        if name in CHOICES and raw not in CHOICES[name]:
            errors.append(f"{name.replace('_', ' ')}: '{raw}' is not a valid choice")
            continue
        try:
            setattr(cfg, name, int(raw or 0) if typ == "int" else float(raw or 0) if typ == "float" else raw)
        except ValueError:
            errors.append(f"{name.replace('_', ' ')}: '{raw}' is not a number")
            continue
    if form.get("_section") == "sports" and not (cfg.cycling_active or cfg.running_active):
        cfg.cycling_active, cfg.running_active = before_active
        errors.append("sports: at least one sport stays active (put the other on hold instead)")
    if form.get("clear_anthropic_api_key") and st.is_owner:
        cfg.anthropic_api_key = ""
    st.save()
    memory.log_changes(st.db, before, cfg, datetime.now().isoformat(timespec="seconds"))
    st.reload()
    return errors


@app.get("/settings")
async def settings_view(request: Request):
    st = athlete(request)
    now = datetime.now()
    cfg = st.cfg
    week = stats.week_start(now.date()).isoformat()
    usage = {"This week": st.db.llm_usage(week, llm.cost), "This month": st.db.llm_usage(now.strftime("%Y-%m-01"), llm.cost),
             "All time": st.db.llm_usage(cost=llm.cost)}
    last_sync = st.db.get("last_sync")
    schedule = {
        "sync": {"last": last_sync["at"][:16].replace("T", " ") + " UTC" if last_sync else None,
                 "next": None if not cfg.sync_interval_min else (SYNC_INTERVALS[cfg.sync_interval_min] if st.source.configured
                                                    else f"Waiting for {st.source_name} to be connected")},
        "zwift": {"last": st.db.get("zwift_refreshed"),
                  "next": None if not cfg.zwift_refresh else ("Off: your indoor app is MyWhoosh" if cfg.workout_app != "zwift"
                                                              else "Daily" if st.zwift.configured else "Zwift not connected")},
        "health": {"last": (st.db.get("health_fetched") or "")[:16].replace("T", " ") or None,
                   "next": ("Every 3 hours from Garmin" if cfg.health_source == "garmin" else "Hourly from Home Assistant")
                   if health.enabled(cfg) else None},
        "notify": {"last": st.db.get("notified"), "next": next_run(now, cfg.notify_time) if cfg.notify_service else None},
        "plan": {"last": st.db.get("auto_plan_ran"), "next": next_run(now, cfg.auto_plan_time, cfg.auto_plan_day) if cfg.auto_plan else None},
        "analysis": {"last": st.db.get("auto_analysis_ran"),
                     "next": next_run(now, cfg.auto_analysis_time, cfg.auto_analysis_day) if cfg.auto_analysis else None},
    }
    notify_options = await st.ha_cached("notify", ha.notify_services)
    own = await st.phones()
    if notify_options and own is not None:  # an athlete only sees their own phone
        notify_options = [o for o in notify_options if own_phone("notify_service", o, own)]
    # A notify.* service HA doesn't have fails every send with a 400: say so, and offer the phone's own service
    notify_missing = bool(notify_options and cfg.notify_service.startswith("notify.") and cfg.notify_service not in notify_options)
    # Pre-filled only when unambiguous (the same name under mobile_app_, or the only phone), since saving the form saves it
    phones = [o for o in notify_options or [] if o.startswith("notify.mobile_app_")]
    same = "notify.mobile_app_" + cfg.notify_service.removeprefix("notify.")
    notify_suggest = (same if same in phones else phones[0] if len(phones) == 1 else None) if notify_missing else None
    if own is not None and not cfg.notify_service and len(phones) == 1:
        notify_suggest = phones[0]  # a new athlete with one phone
    own_cfg = st.own_cfg()
    return render(request, "settings.html", cfg=own_cfg, athlete=st.db.get("strava_athlete"), phones=own,
                  shared_strava="strava_client_id" in st.shared, athletes=athletes_overview(request, now) if st.is_owner else None,
                  notify_options=notify_options, notify_missing=notify_missing, notify_suggest=notify_suggest,
                  usage=usage, schedule=schedule, last_feedback=(st.db.last_llm_call("ride") or "")[:16].replace("T", " "),
                  last_checkin=(st.db.last_llm_call("checkin") or "")[:16].replace("T", " "), sync_intervals=SYNC_INTERVALS, day_names=DAY_NAMES,
                  strava_app=st.strava.has_app, authorize_url=st.strava.authorize_url() if st.strava.has_app else None,
                  redirect_uri=REDIRECT_URI, zwift_profile=st.db.get("zwift_profile"), has_secret={k: bool(getattr(own_cfg, k)) for k in SECRETS})


def athletes_overview(request: Request, now: datetime) -> dict:
    """For the owner's Settings: who's in, what their Claude calls cost this month, and who asked for access."""
    reg: Athletes = request.app.state.athletes
    month = now.strftime("%Y-%m-01")
    rows = []
    for uid, a in reg.listed().items():
        st = reg.others.get(uid)
        who = (st.db.get("strava_athlete") or {}) if st and st.db.get("strava_token") else {}
        rows.append({"uid": uid, "name": a["name"], "added": a["added"],
                     "cost": sum(u["cost"] for u in st.db.llm_usage(month, llm.cost).values()) if st else 0,
                     # what the reset can clear, so the owner sees whose Strava is connected before resetting
                     "strava": f"{who.get('firstname') or ''} {who.get('lastname') or ''}".strip() if who else None,
                     "activities": st.db.count_activities() if st else 0,
                     "health_days": st.db.count_health_days() if st else 0})
    return {"rows": rows, "requests": reg.requests(),
            "own_cost": sum(u["cost"] for u in reg.owner.db.llm_usage(month, llm.cost).values())}


@app.post("/athletes/{uid}/{action}")
async def athletes_action(request: Request, uid: str, action: str):
    st = owner_only(request)
    reg: Athletes = request.app.state.athletes
    if action == "reset":
        form = await request.form()
        ok, message = reg.reset(uid, str(form.get("confirm", "")), "activities" in form, "health" in form, "strava" in form)
        st.flash = ("ok" if ok else "error", message)
        return back(request, "/settings")
    done = {"approve": reg.approve, "dismiss": reg.dismiss, "remove": reg.remove}.get(action)
    if not done or not ATHLETE_ID.fullmatch(uid):
        raise HTTPException(404)
    name = done(uid)
    st.flash = ("ok", {"approve": f"{name} can use Training Coach now.", "dismiss": f"{name}'s request dismissed.",
                       "remove": f"{name} no longer has access. Their data is kept."}[action]) if name else (
        "error", "That athlete or request is gone.")
    return back(request, "/settings")


@app.post("/settings")
async def settings_save(request: Request):
    st = athlete(request)
    old_key = st.cfg.anthropic_api_key
    errors = apply_form(st, await request.form(), await st.phones())
    if st.is_owner:
        request.app.state.athletes.reload_shared()
    if errors:
        st.flash = ("error", "Saved, except: " + "; ".join(errors))
    elif st.is_owner and st.cfg.anthropic_api_key and st.cfg.anthropic_api_key != old_key:
        problem = await llm.check_key(st.cfg.anthropic_api_key)
        st.flash = ("error", f"Saved, but the Claude API key didn't work: {problem}") if problem else (
            "ok", "Saved. Claude API key verified.")
    else:
        st.flash = ("ok", "Settings saved.")
    return back(request, "/settings")


@app.get("/profile")
async def profile_view(request: Request):
    st = athlete(request)
    today = date.today()
    acts = st.db.activities(since_local=(today - timedelta(days=400)).isoformat())
    wkg = stats.dashboard(acts, today, st.cfg)["wkg"] if acts else None
    options = await st.health_options()
    return render(request, "profile.html", cfg=st.cfg, weekdays=WEEKDAYS, levels=LEVELS, wkg=wkg, phones=await st.phones(),
                  notes=st.db.active_notes(today.isoformat()), health_metrics=health.METRICS,
                  history=memory.history(st.db, st.cfg, datetime.now(), memory_ids=True),
                  health_latest=health.summary(st.db, today), health_options=options,
                  # Pre-fill only on first setup, so a field cleared on purpose stays cleared
                  # Pre-fill only on first setup, and only while Apple Health is the source: saving the profile saves them
                  health_found={} if health.entities(st.cfg) or st.cfg.health_source != "apple" else health.detect(options or []),
                  suggested=level_for_wkg(wkg) if wkg else None,
                  # Connections
                  activity_sources=ACTIVITY_SOURCES, health_sources=HEALTH_SOURCES, has_rides=bool(acts),
                  strava_athlete=st.db.get("strava_athlete"), strava_app=st.strava.has_app, shared_strava="strava_client_id" in st.shared,
                  authorize_url=st.strava.authorize_url() if st.strava.has_app else None, redirect_uri=REDIRECT_URI,
                  garmin_key=bool(st.cfg.garmin_api_key), garmin_status=st.garmin.status(),
                  zwift_profile=st.db.get("zwift_profile"), workout_apps=WORKOUT_APPS, connected=st.connected(),
                  mywhoosh_ok=st.mywhoosh.configured, mywhoosh_account=st.mywhoosh.account(),
                  # Sports
                  sport_names=sports.SPORTS, coaches_by_sport={s: coaches.for_sport(s) for s in sports.SPORTS},
                  active_now=st.cfg.active_sports(today), running=stats.running(acts, today, st.cfg),
                  pace_value=stats.fmt_pace(st.cfg.threshold_pace).removesuffix("/km") if st.cfg.threshold_pace else "")


@app.post("/profile")
async def profile_save(request: Request):
    st = athlete(request)
    errors = apply_form(st, await request.form(), await st.phones())
    st.db.set("profile_saved", True)
    recompute(st.db, st.cfg)  # FTP override and LTHR change every ride's TSS
    st.start_job("publish", st.publish())  # MQTT round-trip: don't hold the redirect
    st.flash = ("error", "Saved, except: " + "; ".join(errors)) if errors else ("ok", "Profile saved.")
    return back(request, "/profile")


@app.post("/profile/source")
async def source_save(request: Request):
    """Choose where rides and health data come from. Switching clears what the old source brought in: ride ids from two
    services could collide, and Garmin's HRV is a different measure from Apple's."""
    st = athlete(request)
    form = await request.form()
    act, hea = str(form.get("activity_source", st.cfg.activity_source)), str(form.get("health_source", st.cfg.health_source))
    if act not in ACTIVITY_SOURCES or hea not in HEALTH_SOURCES:
        raise HTTPException(400)
    for new, old in ((act, st.cfg.activity_source), (hea, st.cfg.health_source)):
        if new != old and not st.connected()[new]:
            # Clearing the rides before the new source works would leave the athlete with nothing (CTL 0) in between
            st.flash = ("error", f"Connect {CONNECTION_NAMES[new]} first (below), then switch to it.")
            return back(request, "/profile")
    if any((st.jobs.get(k) or {}).get("running") for k in ("sync", "health")):
        # A running sync or health read would write the old source's data into the cleared tables
        st.flash = ("error", "A sync is running. Try again in a minute.")
        return back(request, "/profile")
    done = []
    if act != st.cfg.activity_source:
        st.db.clear_activities()
        for k in ("last_activity_start", "last_sync"):
            st.db.set(k, None)
        st.db.set("mark_notified", True)  # the rides coming back aren't new: no second round of ride pushes
        done.append(f"Rides now come from {ACTIVITY_SOURCES[act]}. The rides from {ACTIVITY_SOURCES[st.cfg.activity_source]} "
                    "were removed; plans, chat, notes and memory are kept.")
    if hea != st.cfg.health_source:
        st.db.clear_health()
        st.db.set("health_fetched", None)
        done.append(f"Health data now comes from {HEALTH_SOURCES[hea]}.")
    st.cfg.activity_source, st.cfg.health_source = act, hea
    st.save()
    st.reload()
    if done and st.source.configured:
        st.start_job("sync", st.do_sync())
    st.flash = ("ok", " ".join(done) or "Nothing changed.")
    return back(request, "/profile")


@app.post("/profile/garmin")
async def garmin_save(request: Request):
    """Save the athlete's Garmin Personal API key (or, with none given, check the stored one again), check it by minting
    a token, then sync."""
    st = athlete(request)
    key = str((await request.form()).get("garmin_api_key", "")).strip()
    if not key and not st.cfg.garmin_api_key:
        st.flash = ("error", "Paste your Garmin API key first.")
        return back(request, "/profile")
    if key:
        st.cfg.garmin_api_key = key
        st.save()
        st.reload()
    st.garmin.disconnect()  # forget the old token and any refusal: this check decides
    try:
        await st.garmin.check()
    except RateLimited:
        st.flash = ("ok", "Saved. Garmin is busy right now; the key is checked again on the next sync.")
        return back(request, "/profile")
    except (GarminError, httpx.HTTPError) as e:
        st.flash = ("error", f"Saved, but Garmin didn't accept it: {e}")
        return back(request, "/profile")
    st.db.set("health_fetched", None)  # read health on the next tick (a backfill the first time)
    if st.cfg.activity_source == "garmin":
        st.start_job("sync", st.do_sync())
    st.flash = ("ok", "Garmin connected." + (" First sync started: it pulls 12 months of rides." if st.cfg.activity_source == "garmin" else ""))
    return back(request, "/profile")


@app.post("/profile/garmin/disconnect")
async def garmin_disconnect(request: Request):
    st = athlete(request)
    st.cfg.garmin_api_key = ""
    st.save()
    st.reload()
    st.garmin.disconnect()
    st.flash = ("ok", "Garmin disconnected and its key removed. Your synced rides are kept.")
    return back(request, "/profile")


@app.post("/settings/strava/connect")
async def strava_connect(request: Request):
    st = athlete(request)
    pasted = str((await request.form()).get("redirect_url", ""))
    reg: Athletes = request.app.state.athletes

    def taken(who: dict) -> str | None:
        """One Strava account, one athlete: someone signed in to Strava on a shared phone mustn't feed another add-on.
        Checked before the new login is stored, so the connection that was there (the owner's own too) stays."""
        holder = reg.strava_holder(who.get("id"), st)
        if not holder:
            return None
        name = f"{who.get('firstname') or ''} {who.get('lastname') or ''}".strip()
        if st.is_owner:
            return (f"Your Strava account ({name}) is connected in {holder.name}'s add-on. Reset their Strava connection "
                    "under Settings → Athletes first, then connect again.")
        return (f"That Strava account ({name}) is already connected for {holder.name or 'the owner'}. "
                "Sign out of Strava in the browser, sign in as yourself, and connect again.")
    try:
        who = await st.strava.exchange_code(pasted, refuse=taken)
    except (ValueError, KeyError) as e:
        st.flash = ("error", str(e))
        return back(request, "/profile")
    name = f"{who.get('firstname', '')} {who.get('lastname', '')}".strip()
    if st.cfg.activity_source != "strava":  # connected ahead of switching: rides still come from the other source
        st.flash = ("ok", f"Connected to Strava as {name}. Pick Strava under Rides to use it.")
        return back(request, "/profile")
    st.flash = ("ok", f"Connected to Strava as {name}. "
                      "First sync started — it pulls 12 months of rides, power detail fills in over a few hours.")
    st.start_job("sync", st.do_sync())
    return back(request, "/profile")


@app.post("/settings/strava/disconnect")
async def strava_disconnect(request: Request):
    st = athlete(request)
    st.strava.disconnect()
    st.flash = ("ok", "Strava disconnected. Your synced rides are kept.")
    return back(request, "/profile")


@app.post("/settings/zwift/connect")
async def zwift_connect(request: Request):
    st = athlete(request)
    form = await request.form()
    try:
        profile = await st.zwift.connect(str(form.get("zwift_username", "")), str(form.get("zwift_password", "")))
    except (ZwiftError, httpx.HTTPError) as e:
        st.zwift.disconnect()
        st.flash = ("error", f"Zwift connection failed: {e}")
    else:
        st.flash = ("ok", f"Connected to Zwift as {profile.get('firstName', '')} {profile.get('lastName', '')}. "
                          "Zwift sessions on the Plan page now have a “Send to Zwift” button.")
    return back(request, "/profile")


@app.post("/profile/indoor")
async def indoor_app_save(request: Request):
    """Which app "Send to …" on the Plan page sends indoor sessions to. Nothing to clear: the plans stay the same."""
    st = athlete(request)
    app_key = str((await request.form()).get("workout_app", ""))
    if app_key not in WORKOUT_APPS:
        raise HTTPException(400)
    if app_key != st.cfg.workout_app and not st.connected()[app_key]:
        st.flash = ("error", f"Connect {WORKOUT_APPS[app_key]} first (below), then switch to it.")
        return back(request, "/profile")
    st.cfg.workout_app = app_key
    st.save()
    st.reload()
    st.flash = ("ok", f"Indoor sessions go to {WORKOUT_APPS[app_key]} now.")
    return back(request, "/profile")


@app.post("/profile/mywhoosh/connect")
async def mywhoosh_connect(request: Request):
    st = athlete(request)
    form = await request.form()
    try:
        account = await st.mywhoosh.connect(str(form.get("mywhoosh_email", "")), str(form.get("mywhoosh_password", "")))
    except (MyWhooshError, httpx.HTTPError) as e:
        st.mywhoosh.disconnect()
        st.flash = ("error", f"MyWhoosh sign-in failed: {e}")
    else:
        st.flash = ("ok", f"Connected to MyWhoosh as {account['email']}. Indoor sessions on the Plan page now have a "
                          "“Send to MyWhoosh” button.")
    return back(request, "/profile")


@app.post("/profile/mywhoosh/disconnect")
async def mywhoosh_disconnect(request: Request):
    st = athlete(request)
    st.mywhoosh.disconnect()
    st.db.set("mywhoosh_account", None)  # signed out on purpose: Profile mustn't say the login expired
    st.flash = ("ok", "MyWhoosh disconnected. Workouts already sent stay in My Workouts.")
    return back(request, "/profile")


@app.post("/settings/zwift/disconnect")
async def zwift_disconnect(request: Request):
    st = athlete(request)
    st.zwift.disconnect()
    st.flash = ("ok", "Zwift disconnected. Workouts already sent stay in your Zwift account.")
    return back(request, "/profile")


@app.get("/healthz")
async def healthz():
    return {"ok": True}
