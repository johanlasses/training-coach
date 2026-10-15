"""FastAPI app served through Home Assistant ingress."""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import markdown as md
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import analysis, chat, coaches, ftptest, ha, health, stats, workouts
from . import coach as coach_mod
from . import llm
from .config import DATA_DIR, FIELD_TYPES, LEVELS, SECRETS, SYNC_INTERVALS, WEEKDAYS, level_for_wkg, load_config, save_config
from .db import DB
from .strava import REDIRECT_URI, StravaClient
from .sync import recompute, sync
from .zwift import ZwiftClient, ZwiftError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("training_coach")

HERE = Path(__file__).parent
DUE_WINDOW = timedelta(hours=3)
HEALTH_EVERY = timedelta(hours=1)  # a local HA read, no external API


class State:
    def __init__(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.db = DB(DATA_DIR / "training.db")
        self.jobs: dict[str, dict] = {}
        self.flash: tuple[str, str] | None = None   # (kind, message) shown once on the next page
        self._tasks: set[asyncio.Task] = set()
        self.reload()

    def reload(self) -> None:
        self.cfg = load_config(self.db)
        self.strava = StravaClient(self.db, self.cfg.strava_client_id, self.cfg.strava_client_secret)
        self.zwift = ZwiftClient(self.db)

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
        if not self.strava.configured:
            raise RuntimeError("Strava is not connected — see Settings")
        await sync(self.db, self.strava, self.cfg)
        await self.publish()

    def snapshot(self) -> dict:
        today = date.today()
        snap = stats.snapshot(self.db.activities(), today, self.cfg)
        snap["ftp_tests"] = self.recent_tests(today)
        snap["coach_notes"] = [{k: n[k] for k in ("kind", "start", "end", "text")}
                               for n in self.db.active_notes(today.isoformat())]
        if health.entities(self.cfg):
            snap["health"] = health.summary(self.db, today)
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

    async def health_options(self) -> list[dict] | None:
        """Apple Health-looking sensors for Profile's pickers; None when HA can't be read. Cached for 10 minutes,
        since it means fetching every entity state in HA."""
        cached = getattr(self, "_health_options", None)
        if cached and time.monotonic() - cached[0] < 600:
            return cached[1]
        states = await ha.states()
        options = health.candidates(states) if states else None
        if options is not None:
            self._health_options = (time.monotonic(), options)
        return options

    async def do_chat(self) -> None:
        await chat.respond(self.db, self.cfg, self.snapshot(), date.today())

    async def do_analysis(self, coach_key: str) -> None:
        await analysis.run_analysis(self.db, self.cfg, self.snapshot(), "manual", coach_key)

    async def publish(self) -> None:
        today = date.today()
        d = stats.dashboard(self.db.activities(since_local=(today - timedelta(days=400)).isoformat()), today, self.cfg)
        session = today_session(self.db, today)
        await ha.publish_sensors({
            "ftp": d["ftp"], "wkg": d["wkg"], "ctl": d["ctl"], "atl": d["atl"], "tsb": d["tsb"],
            "week_hours": d["this_week"]["hours"], "week_km": d["this_week"]["km"],
            "today_session": session["title"] if session else "Rest",
        })

    async def do_auto_plan(self, week_start: date) -> None:
        try:
            await self.do_plan(week_start, self.cfg.default_coach)
        except Exception as e:
            await ha.notify(self.cfg.notify_service, "Next week's plan failed", str(e)[:180])
            raise
        await ha.notify(self.cfg.notify_service, "Next week's plan is ready",
                        f"{coaches.name_of(self.cfg.default_coach)} planned the week of {week_start.strftime('%-d %b')}.")

    async def do_auto_analysis(self) -> None:
        try:
            await analysis.run_analysis(self.db, self.cfg, self.snapshot(), "scheduled")
        except Exception as e:
            await ha.notify(self.cfg.notify_service, "Weekly analysis failed", str(e)[:180])
            raise
        await ha.notify(self.cfg.notify_service, "Your weekly analysis is ready", "Open Training → Analysis.")

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

    async def scheduler(self) -> None:
        """Minute tick. Every job, and when it runs, is set on Settings → Schedules. Claude jobs are off by default."""
        while True:
            try:
                now = datetime.now()
                cfg = self.cfg
                last = self.db.get("last_sync")
                if self.strava.configured and cfg.sync_interval_min and (
                        not last or datetime.now(timezone.utc) - datetime.fromisoformat(last["at"])
                        > timedelta(minutes=cfg.sync_interval_min)):
                    self.start_job("sync", self.do_sync())
                if cfg.zwift_refresh and self.zwift.configured and self.db.get("zwift_refreshed") != now.date().isoformat():
                    # Keycloak drops idle sessions; a daily refresh keeps the Zwift login alive.
                    self.db.set("zwift_refreshed", now.date().isoformat())
                    try:
                        await self.zwift.refresh()
                    except ZwiftError as e:
                        log.warning("Zwift token refresh: %s", e)
                fetched = self.db.get("health_fetched")
                if health.entities(cfg) and ha.available() and (
                        not fetched or now - datetime.fromisoformat(fetched) >= HEALTH_EVERY):
                    self.db.set("health_fetched", now.isoformat(timespec="seconds"))
                    self.start_job("health", health.fetch(self.db, cfg, now.date()))
                if cfg.notify_service and self.due("notified", now, cfg.notify_time):
                    self.ran("notified", now)
                    s = today_session(self.db, now.date())
                    if s:
                        await ha.notify(cfg.notify_service, f"Today: {s['title']}",
                                        f"{s['duration_min']} min · {s['description'][:180]}")
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


def today_session(db: DB, today: date) -> dict | None:
    plan = db.active_plan(stats.week_start(today).isoformat())
    if not plan:
        return None
    return next((d for d in plan["payload"]["days"] if d["date"] == today.isoformat() and d["kind"] != "rest"), None)


@asynccontextmanager
async def lifespan(app: FastAPI):
    st = State()
    app.state.st = st
    task = asyncio.create_task(st.scheduler())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")


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
templates.env.filters["coach_name"] = coaches.name_of
templates.env.filters["coach_short"] = coaches.short_name_of


def base(request: Request) -> str:
    """Ingress prefix (e.g. /api/hassio_ingress/<token>); empty when accessed directly."""
    return request.headers.get("X-Ingress-Path", "").rstrip("/")


def render(request: Request, name: str, **ctx) -> Response:
    st: State = request.app.state.st
    flash, st.flash = st.flash, None
    return templates.TemplateResponse(request, name, {
        "base": base(request), "jobs": st.jobs, "last_sync": st.db.get("last_sync"), "flash": flash,
        "strava_ok": st.strava.configured, "zwift_ok": st.zwift.configured, "ai_ok": bool(st.cfg.anthropic_api_key), **ctx})


def back(request: Request, path: str) -> RedirectResponse:
    return RedirectResponse(f"{base(request)}{path}", status_code=303)


@app.get("/")
async def home(request: Request):
    st: State = request.app.state.st
    today = date.today()
    acts = st.db.activities(since_local=(today - timedelta(days=400)).isoformat())
    return render(request, "home.html", d=stats.dashboard(acts, today, st.cfg), feed=acts[:30],
                  session=today_session(st.db, today))


@app.get("/activity/{activity_id}")
async def activity(request: Request, activity_id: int):
    st: State = request.app.state.st
    a = st.db.activity(activity_id)
    if not a:
        raise HTTPException(404)
    day = date.fromisoformat(a["start_date_local"][:10])
    acts = st.db.activities(since_local=(day - timedelta(days=95)).isoformat())
    from .metrics import ZONES, zone_seconds
    zones = list(zip([z for z, _ in ZONES], zone_seconds(a["power_hist"], a["ftp_used"]))) if a.get("power_hist") else []
    return render(request, "activity.html", a=a, curve90=stats.power_curve(acts, day, 90), zones=zones)


@app.get("/plan")
async def plan_view(request: Request, week: str | None = None, coach: str | None = None):
    st: State = request.app.state.st
    today = date.today()
    ws = date.fromisoformat(week) if week else stats.week_start(today)
    plans = {k or "": p for k, p in st.db.week_plans(ws.isoformat()).items()}
    active = st.db.active_plan(ws.isoformat())
    if coach not in plans and coach not in coaches.COACHES:
        coach = (active["coach"] or "") if active else st.cfg.default_coach
    plan = plans.get(coach)
    acts = st.db.activities(since_local=ws.isoformat())
    by_day: dict[str, list] = {}
    for a in acts:
        by_day.setdefault(a["start_date_local"][:10], []).append(a)
    status, tests = {}, {}
    if plan:
        for d in plan["payload"]["days"]:
            status[d["date"]] = "upcoming" if d["date"] > today.isoformat() else coach_mod.compliance(d, by_day.get(d["date"], []))
            results = [r for a in by_day.get(d["date"], []) if (r := ftptest.estimate(d.get("test_protocol", "none"), a.get("mmp")))]
            if results:
                tests[d["date"]] = max(results)
    tabs = list(coaches.COACHES) + ([""] if "" in plans else [])
    return render(request, "plan.html", plan=plan, plans=plans, coach=coach, tabs=tabs,
                  active_id=active["id"] if active else None, week=ws, prev=ws - timedelta(days=7),
                  next=ws + timedelta(days=7), today=today.isoformat(), status=status, by_day=by_day, tests=tests,
                  applied_tests={t["date"] for t in st.cfg.ftp_tests},
                  zwift_uploads=st.db.get("zwift_uploads") or {})


def zwift_day(st: State, plan_id: int, day: str) -> tuple[dict, dict, str]:
    """(plan, day, .zwo XML) for a planned Zwift session; 404 otherwise. Cheers come from the plan's coach."""
    plan = st.db.plan(plan_id)
    d = next((d for d in (plan or {}).get("payload", {}).get("days", []) if d["date"] == day and d["kind"] == "zwift"), None)
    if not d:
        raise HTTPException(404)
    c = coaches.get(plan["coach"])
    name = f"{date.fromisoformat(day).strftime('%a %-d %b')} · {d['title']}"
    return plan, d, workouts.to_zwo(name, d["description"], d["steps"], c.cheers, c.last_rep)


@app.get("/plan/{plan_id}/{day}.zwo")
async def zwo(request: Request, plan_id: int, day: str):
    _, d, xml = zwift_day(request.app.state.st, plan_id, day)
    return Response(xml, media_type="application/xml",
                    headers={"Content-Disposition": f'attachment; filename="{workouts.filename(day, d["title"])}"'})


@app.post("/plan/{plan_id}/{day}/zwift")
async def zwift_upload(request: Request, plan_id: int, day: str):
    st: State = request.app.state.st
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
    return back(request, f"/plan?week={plan['week_start']}&coach={plan['coach'] or ''}")


@app.get("/analysis")
async def analysis_view(request: Request, id: int | None = None):
    st: State = request.app.state.st
    a = st.db.analysis(id)
    html = md.markdown(a["markdown"], extensions=["tables"]) if a else None
    return render(request, "analysis.html", a=a, html=html, history=st.db.analyses(),
                  default_coach=st.cfg.default_coach)


@app.post("/sync")
async def sync_now(request: Request):
    st: State = request.app.state.st
    if st.strava.paused_until > time.time():
        until = datetime.fromtimestamp(st.strava.paused_until, timezone.utc).strftime("%H:%M")
        st.flash = ("ok", f"Strava's rate limit was reached. Sync resumes on its own at {until} UTC.")
        return back(request, "/")
    st.start_job("sync", st.do_sync())
    return back(request, "/")


@app.post("/plan")
async def plan_now(request: Request, week: str | None = None):
    st: State = request.app.state.st
    ws = date.fromisoformat(week) if week else stats.week_start(date.today())
    key = coaches.get((await request.form()).get("coach") or st.cfg.default_coach).key
    st.start_job("plan", st.do_plan(ws, key))
    return back(request, f"/plan?week={ws.isoformat()}&coach={key}")


@app.post("/plan/{plan_id}/activate")
async def plan_activate(request: Request, plan_id: int):
    st: State = request.app.state.st
    plan = st.db.plan(plan_id)
    if not plan:
        raise HTTPException(404)
    st.db.set_active_plan(plan["week_start"], plan_id)
    await st.publish()
    return back(request, f"/plan?week={plan['week_start']}&coach={plan['coach'] or ''}")


@app.get("/coach")
async def coach_view(request: Request):
    st: State = request.app.state.st
    today = date.today()
    messages = st.db.chat()
    active = st.db.active_plan(stats.week_start(today).isoformat())
    for m in messages:  # old → new title per changed day, for the proposal diff
        if m["proposal"]:
            old = {d["date"]: d for d in (st.db.plan(m["proposal"]["plan_id"]) or {}).get("payload", {}).get("days", [])}
            m["diff"] = [(d, old.get(d["date"])) for d in m["proposal"]["days"]]
            m["live"] = (not m["applied_plan_id"] and active and active["id"] == m["proposal"]["plan_id"]
                         and all(d["date"] >= today.isoformat() for d in m["proposal"]["days"]))
    return render(request, "coach.html", messages=messages, coach=chat.chat_coach(st.db, st.cfg, today),
                  notes=st.db.active_notes(today.isoformat()), has_plan=bool(active))


@app.post("/coach")
async def coach_send(request: Request):
    st: State = request.app.state.st
    text = str((await request.form()).get("message", "")).strip()[:4000]
    if (st.jobs.get("chat") or {}).get("running"):
        st.flash = ("error", "The coach is still answering your last message. Send this one when the reply is in.")
    elif text:
        st.db.add_chat(datetime.now().isoformat(timespec="seconds"), "user", chat.chat_coach(st.db, st.cfg, date.today()).key, text)
        st.start_job("chat", st.do_chat())
    return back(request, "/coach#end")


@app.post("/coach/{msg_id}/apply")
async def coach_apply(request: Request, msg_id: int):
    st: State = request.app.state.st
    msg = st.db.chat_message(msg_id)
    if not msg:
        raise HTTPException(404)
    plan_id, text = chat.apply(st.db, msg, date.today())
    st.flash = ("ok" if plan_id else "error", text)
    if plan_id:
        await st.publish()
    return back(request, "/coach#end")


@app.post("/coach/clear")
async def coach_clear(request: Request):
    st: State = request.app.state.st
    st.db.clear_chat()
    st.flash = ("ok", "Conversation cleared. Coach notes are kept.")
    return back(request, "/coach")


@app.post("/notes/{note_id}/delete")
async def note_delete(request: Request, note_id: int):
    st: State = request.app.state.st
    st.db.delete_note(note_id)
    return back(request, "/profile" if (await request.form()).get("from") == "profile" else "/coach")


@app.post("/ftp/apply")
async def ftp_apply(request: Request):
    """Records a field test's FTP from the test date on. Rides before it keep the FTP they had."""
    st: State = request.app.state.st
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
        save_config(st.db, st.cfg)
        st.reload()
        recompute(st.db, st.cfg)
        await st.publish()
        st.flash = ("ok", f"FTP {ftp} W from your test on {day.strftime('%-d %b')}. It counts from that day for 12 weeks, "
                          "then the estimate from your best efforts takes over." + (
                              " Your FTP override on Profile still wins until you clear it." if st.cfg.ftp_override else ""))
    return back(request, f"/plan?week={form.get('week', '')}")


@app.post("/ftp/tests/delete")
async def ftp_test_delete(request: Request, date_: str = Query(alias="date")):
    st: State = request.app.state.st
    st.cfg.ftp_tests = [t for t in st.cfg.ftp_tests if t["date"] != date_]
    save_config(st.db, st.cfg)
    st.reload()
    recompute(st.db, st.cfg)
    await st.publish()
    st.flash = ("ok", "Test result removed.")
    return back(request, "/profile")


@app.post("/analysis")
async def analysis_now(request: Request):
    st: State = request.app.state.st
    st.start_job("analysis", st.do_analysis(coaches.get((await request.form()).get("coach") or st.cfg.default_coach).key))
    return back(request, "/analysis")


DAY_NAMES = dict(zip(WEEKDAYS, ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]))
CHOICES = {"athlete_level": LEVELS, "default_coach": coaches.COACHES, "sync_interval_min": {str(k) for k in SYNC_INTERVALS},
           "auto_plan_day": DAY_NAMES, "auto_analysis_day": DAY_NAMES}
TIMES = {"notify_time", "auto_plan_time", "auto_analysis_time"}


def valid_time(raw: str) -> bool:
    try:
        datetime.strptime(raw, "%H:%M")
        return True
    except ValueError:
        return False


def apply_form(st: State, form) -> list[str]:
    """Apply the config fields present in a submitted form and persist. Returns parse errors.

    Fields missing from the form are untouched, so each page only changes its own settings.
    """
    cfg = st.cfg
    errors = []
    for name, typ in FIELD_TYPES.items():
        if name == "available_days":
            if "availability" in form:  # only present when that section was submitted
                cfg.available_days = [d for d in WEEKDAYS if d in form.getlist("available_days")]
            continue
        if typ == "bool":
            if form.get("_section") == "schedules":  # unticked checkboxes are absent from the form
                setattr(cfg, name, name in form)
            continue
        if name not in form:
            continue
        raw = str(form[name]).strip()
        if name in TIMES and raw and not valid_time(raw):
            errors.append(f"{name.replace('_', ' ')}: '{raw}' is not a time like 07:00")
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
    if form.get("clear_anthropic_api_key"):
        cfg.anthropic_api_key = ""
    save_config(st.db, cfg)
    st.reload()
    return errors


@app.get("/settings")
async def settings_view(request: Request):
    st: State = request.app.state.st
    now = datetime.now()
    cfg = st.cfg
    week = stats.week_start(now.date()).isoformat()
    usage = {"This week": st.db.llm_usage(week), "This month": st.db.llm_usage(now.strftime("%Y-%m-01")),
             "All time": st.db.llm_usage()}
    last_sync = st.db.get("last_sync")
    schedule = {
        "sync": {"last": last_sync["at"][:16].replace("T", " ") + " UTC" if last_sync else None,
                 "next": None if not cfg.sync_interval_min else (SYNC_INTERVALS[cfg.sync_interval_min] if st.strava.configured
                                                    else "Waiting for Strava to be connected")},
        "zwift": {"last": st.db.get("zwift_refreshed"),
                  "next": None if not cfg.zwift_refresh else ("Daily" if st.zwift.configured else "Zwift not connected")},
        "health": {"last": (st.db.get("health_fetched") or "")[:16].replace("T", " ") or None,
                   "next": "Hourly" if health.entities(cfg) else None},
        "notify": {"last": st.db.get("notified"), "next": next_run(now, cfg.notify_time) if cfg.notify_service else None},
        "plan": {"last": st.db.get("auto_plan_ran"), "next": next_run(now, cfg.auto_plan_time, cfg.auto_plan_day) if cfg.auto_plan else None},
        "analysis": {"last": st.db.get("auto_analysis_ran"),
                     "next": next_run(now, cfg.auto_analysis_time, cfg.auto_analysis_day) if cfg.auto_analysis else None},
    }
    return render(request, "settings.html", cfg=st.cfg, athlete=st.db.get("strava_athlete"),
                  usage=usage, schedule=schedule, sync_intervals=SYNC_INTERVALS, day_names=DAY_NAMES,
                  strava_app=st.strava.has_app, authorize_url=st.strava.authorize_url() if st.strava.has_app else None,
                  redirect_uri=REDIRECT_URI, zwift_profile=st.db.get("zwift_profile"), has_secret={k: bool(getattr(st.cfg, k)) for k in SECRETS})


@app.post("/settings")
async def settings_save(request: Request):
    st: State = request.app.state.st
    old_key = st.cfg.anthropic_api_key
    errors = apply_form(st, await request.form())
    if errors:
        st.flash = ("error", "Saved, except: " + "; ".join(errors))
    elif st.cfg.anthropic_api_key and st.cfg.anthropic_api_key != old_key:
        problem = await llm.check_key(st.cfg.anthropic_api_key)
        st.flash = ("error", f"Saved, but the Claude API key didn't work: {problem}") if problem else (
            "ok", "Saved. Claude API key verified.")
    else:
        st.flash = ("ok", "Settings saved.")
    return back(request, "/settings")


@app.get("/profile")
async def profile_view(request: Request):
    st: State = request.app.state.st
    today = date.today()
    acts = st.db.activities(since_local=(today - timedelta(days=400)).isoformat())
    wkg = stats.dashboard(acts, today, st.cfg)["wkg"] if acts else None
    return render(request, "profile.html", cfg=st.cfg, weekdays=WEEKDAYS, levels=LEVELS, wkg=wkg,
                  notes=st.db.active_notes(today.isoformat()), health_metrics=health.METRICS,
                  health_latest=health.summary(st.db, today), health_options=await st.health_options(),
                  suggested=level_for_wkg(wkg) if wkg else None)


@app.post("/profile")
async def profile_save(request: Request):
    st: State = request.app.state.st
    errors = apply_form(st, await request.form())
    recompute(st.db, st.cfg)  # FTP override and LTHR change every ride's TSS
    await st.publish()
    st.flash = ("error", "Saved, except: " + "; ".join(errors)) if errors else ("ok", "Profile saved.")
    return back(request, "/profile")


@app.post("/settings/strava/connect")
async def strava_connect(request: Request):
    st: State = request.app.state.st
    pasted = str((await request.form()).get("redirect_url", ""))
    try:
        athlete = await st.strava.exchange_code(pasted)
    except (ValueError, KeyError) as e:
        st.flash = ("error", str(e))
        return back(request, "/settings")
    st.flash = ("ok", f"Connected to Strava as {athlete.get('firstname', '')} {athlete.get('lastname', '')}. "
                      "First sync started — it pulls 12 months of rides, power detail fills in over a few hours.")
    st.start_job("sync", st.do_sync())
    return back(request, "/settings")


@app.post("/settings/strava/disconnect")
async def strava_disconnect(request: Request):
    st: State = request.app.state.st
    st.strava.disconnect()
    st.flash = ("ok", "Strava disconnected. Your synced rides are kept.")
    return back(request, "/settings")


@app.post("/settings/zwift/connect")
async def zwift_connect(request: Request):
    st: State = request.app.state.st
    form = await request.form()
    try:
        profile = await st.zwift.connect(str(form.get("zwift_username", "")), str(form.get("zwift_password", "")))
    except (ZwiftError, httpx.HTTPError) as e:
        st.zwift.disconnect()
        st.flash = ("error", f"Zwift connection failed: {e}")
    else:
        st.flash = ("ok", f"Connected to Zwift as {profile.get('firstName', '')} {profile.get('lastName', '')}. "
                          "Zwift sessions on the Plan page now have a “Send to Zwift” button.")
    return back(request, "/settings")


@app.post("/settings/zwift/disconnect")
async def zwift_disconnect(request: Request):
    st: State = request.app.state.st
    st.zwift.disconnect()
    st.flash = ("ok", "Zwift disconnected. Workouts already sent stay in your Zwift account.")
    return back(request, "/settings")


@app.get("/healthz")
async def healthz():
    return {"ok": True}
