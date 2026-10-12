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
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import analysis, coaches, ha, stats, workouts
from . import coach as coach_mod
from . import llm
from .config import DATA_DIR, FIELD_TYPES, LEVELS, SECRETS, WEEKDAYS, level_for_wkg, load_config, save_config
from .db import DB
from .strava import REDIRECT_URI, StravaClient
from .sync import recompute, sync
from .zwift import ZwiftClient, ZwiftError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("training_coach")

HERE = Path(__file__).parent
SYNC_EVERY = timedelta(minutes=30)


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
        return stats.snapshot(self.db.activities(), date.today(), self.cfg)

    async def do_plan(self, week_start: date, coach_key: str) -> None:
        await coach_mod.generate_plan(self.db, self.cfg, self.snapshot(), week_start, date.today(), coach_key)
        await self.publish()

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

    async def scheduler(self) -> None:
        """Minute tick: sync every 30 min, notify 07:00. Plans and analyses only run when the user asks."""
        while True:
            try:
                now = datetime.now()
                last = self.db.get("last_sync")
                if self.strava.configured and (
                        not last or datetime.now(timezone.utc) - datetime.fromisoformat(last["at"]) > SYNC_EVERY):
                    self.start_job("sync", self.do_sync())
                if self.zwift.configured and self.db.get("zwift_refreshed") != now.date().isoformat():
                    # Keycloak drops idle sessions; a daily refresh keeps the Zwift login alive.
                    self.db.set("zwift_refreshed", now.date().isoformat())
                    try:
                        await self.zwift.refresh()
                    except ZwiftError as e:
                        log.warning("Zwift token refresh: %s", e)
                if now.hour >= 7 and self.db.get("notified") != now.date().isoformat():
                    self.db.set("notified", now.date().isoformat())
                    s = today_session(self.db, now.date())
                    if s:
                        await ha.notify(self.cfg.notify_service, f"Today: {s['title']}",
                                        f"{s['duration_min']} min · {s['description'][:180]}")
            except Exception:  # noqa: BLE001 - keep the scheduler alive
                log.exception("Scheduler tick failed")
            await asyncio.sleep(60)


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
    status = {}
    if plan:
        for d in plan["payload"]["days"]:
            status[d["date"]] = "upcoming" if d["date"] > today.isoformat() else coach_mod.compliance(d, by_day.get(d["date"], []))
    tabs = list(coaches.COACHES) + ([""] if "" in plans else [])
    return render(request, "plan.html", plan=plan, plans=plans, coach=coach, tabs=tabs,
                  active_id=active["id"] if active else None, week=ws, prev=ws - timedelta(days=7),
                  next=ws + timedelta(days=7), today=today.isoformat(), status=status, by_day=by_day,
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


@app.post("/analysis")
async def analysis_now(request: Request):
    st: State = request.app.state.st
    st.start_job("analysis", st.do_analysis(coaches.get((await request.form()).get("coach") or st.cfg.default_coach).key))
    return back(request, "/analysis")


CHOICES = {"athlete_level": LEVELS, "default_coach": coaches.COACHES}


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
        if name not in form:
            continue
        raw = str(form[name]).strip()
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
    return render(request, "settings.html", cfg=st.cfg, athlete=st.db.get("strava_athlete"),
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
