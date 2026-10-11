"""FastAPI app served through Home Assistant ingress."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import markdown as md
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import analysis, coach, ha, stats, workouts
from . import llm
from .config import DATA_DIR, FIELD_TYPES, SECRETS, WEEKDAYS, load_config, save_config
from .db import DB
from .strava import REDIRECT_URI, StravaClient
from .sync import recompute, sync

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

    async def snapshot(self) -> dict:
        forecast = await ha.daily_forecast(self.cfg.weather_entity)
        return stats.snapshot(self.db.activities(), date.today(), self.cfg, forecast)

    async def do_plan(self, week_start: date) -> None:
        await coach.generate_plan(self.db, self.cfg, await self.snapshot(), week_start, date.today())
        await self.publish()

    async def do_analysis(self, kind: str) -> None:
        await analysis.run_analysis(self.db, self.cfg, await self.snapshot(), kind)

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
        """Minute tick: sync every 30 min, plan Sun 18:00, analysis Mon 06:00, notify 07:00."""
        while True:
            try:
                now = datetime.now()
                last = self.db.get("last_sync")
                if self.strava.configured and (
                        not last or datetime.now(timezone.utc) - datetime.fromisoformat(last["at"]) > SYNC_EVERY):
                    self.start_job("sync", self.do_sync())
                if self.cfg.anthropic_api_key:
                    nxt = stats.week_start(now.date()) + timedelta(days=7)
                    if now.weekday() == 6 and now.hour >= 18 and not self.db.latest_plan(nxt.isoformat()):
                        self.start_job("plan", self.do_plan(nxt))
                    monday = stats.week_start(now.date()).isoformat()
                    latest = self.db.analysis()
                    if now.hour >= 6 and (not latest or latest["created_at"] < monday) and self.db.activities(limit=1):
                        self.start_job("analysis", self.do_analysis("weekly"))
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
    plan = db.latest_plan(stats.week_start(today).isoformat())
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


def base(request: Request) -> str:
    """Ingress prefix (e.g. /api/hassio_ingress/<token>); empty when accessed directly."""
    return request.headers.get("X-Ingress-Path", "").rstrip("/")


def render(request: Request, name: str, **ctx) -> Response:
    st: State = request.app.state.st
    flash, st.flash = st.flash, None
    return templates.TemplateResponse(request, name, {
        "base": base(request), "jobs": st.jobs, "last_sync": st.db.get("last_sync"), "flash": flash,
        "strava_ok": st.strava.configured, "ai_ok": bool(st.cfg.anthropic_api_key), **ctx})


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
async def plan_view(request: Request, week: str | None = None):
    st: State = request.app.state.st
    today = date.today()
    ws = date.fromisoformat(week) if week else stats.week_start(today)
    plan = st.db.latest_plan(ws.isoformat())
    acts = st.db.activities(since_local=ws.isoformat())
    by_day: dict[str, list] = {}
    for a in acts:
        by_day.setdefault(a["start_date_local"][:10], []).append(a)
    status = {}
    if plan:
        for d in plan["payload"]["days"]:
            status[d["date"]] = "upcoming" if d["date"] > today.isoformat() else coach.compliance(d, by_day.get(d["date"], []))
    return render(request, "plan.html", plan=plan, week=ws, prev=ws - timedelta(days=7), next=ws + timedelta(days=7),
                  today=today.isoformat(), status=status, by_day=by_day)


@app.get("/plan/{plan_id}/{day}.zwo")
async def zwo(request: Request, plan_id: int, day: str):
    st: State = request.app.state.st
    plan = st.db.plan(plan_id)
    d = next((d for d in (plan or {}).get("payload", {}).get("days", []) if d["date"] == day and d["kind"] == "zwift"), None)
    if not d:
        raise HTTPException(404)
    xml = workouts.to_zwo(d["title"], d["description"], d["steps"])
    return Response(xml, media_type="application/xml",
                    headers={"Content-Disposition": f'attachment; filename="{workouts.filename(day, d["title"])}"'})


@app.get("/analysis")
async def analysis_view(request: Request, id: int | None = None):
    st: State = request.app.state.st
    a = st.db.analysis(id)
    html = md.markdown(a["markdown"], extensions=["tables"]) if a else None
    return render(request, "analysis.html", a=a, html=html, history=st.db.analyses())


@app.post("/sync")
async def sync_now(request: Request):
    request.app.state.st.start_job("sync", request.app.state.st.do_sync())
    return back(request, "/")


@app.post("/plan")
async def plan_now(request: Request, week: str | None = None):
    st: State = request.app.state.st
    ws = date.fromisoformat(week) if week else stats.week_start(date.today())
    st.start_job("plan", st.do_plan(ws))
    return back(request, f"/plan?week={ws.isoformat()}")


@app.post("/analysis")
async def analysis_now(request: Request):
    st: State = request.app.state.st
    st.start_job("analysis", st.do_analysis("manual"))
    return back(request, "/analysis")


@app.get("/settings")
async def settings_view(request: Request):
    st: State = request.app.state.st
    return render(request, "settings.html", cfg=st.cfg, weekdays=WEEKDAYS, athlete=st.db.get("strava_athlete"),
                  strava_app=st.strava.has_app, authorize_url=st.strava.authorize_url() if st.strava.has_app else None,
                  redirect_uri=REDIRECT_URI, has_secret={k: bool(getattr(st.cfg, k)) for k in SECRETS})


@app.post("/settings")
async def settings_save(request: Request):
    st: State = request.app.state.st
    form = await request.form()
    cfg = st.cfg
    old_key = cfg.anthropic_api_key
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
        try:
            setattr(cfg, name, int(raw or 0) if typ == "int" else float(raw or 0) if typ == "float" else raw)
        except ValueError:
            errors.append(f"{name.replace('_', ' ')}: '{raw}' is not a number")
    if form.get("clear_anthropic_api_key"):
        cfg.anthropic_api_key = ""
    save_config(st.db, cfg)
    st.reload()
    recompute(st.db, st.cfg)

    if errors:
        st.flash = ("error", "Saved, except: " + "; ".join(errors))
    elif st.cfg.anthropic_api_key and st.cfg.anthropic_api_key != old_key:
        problem = await llm.check_key(st.cfg.anthropic_api_key)
        st.flash = ("error", f"Saved, but the Claude API key didn't work: {problem}") if problem else (
            "ok", "Saved. Claude API key verified.")
    else:
        st.flash = ("ok", "Settings saved.")
    return back(request, "/settings")


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


@app.get("/healthz")
async def healthz():
    return {"ok": True}
