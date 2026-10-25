"""End-to-end checks against the running dev stack (`dev/devha.sh up` first). Drives the add-on only through its
ingress URL, like the HA frontend does, and inspects HA Core, the fakes and the add-on's DB and log.

Usage: smoke.py [--mobile] [--keep]   (--mobile also runs tools/mobile_check.py against the ingress URL;
--keep skips the reset of the add-on's data, which every run does by default)"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

HERE = Path(__file__).parent
PREFIX = "/api/hassio_ingress/dev"
INGRESS = f"http://localhost:8124{PREFIX}"
STRAVA, ANTHROPIC, ZWIFT, SUP = "http://localhost:8101", "http://localhost:8102", "http://localhost:8103", "http://localhost:8124"
HA = "http://localhost:8123"
COMPOSE = ["docker", "compose", "-f", str(HERE / "compose.yaml")]
PAGES = ["/", "/plan", "/coach", "/analysis", "/profile", "/settings"]
NOTIFY = "notify.mobile_app_dev_iphone"
HEALTH_FIELDS = {"health_rhr_entity": "sensor.dev_iphone_resting_heart_rate", "health_hrv_entity": "sensor.dev_iphone_heart_rate_variability",
                 "health_sleep_entity": "sensor.dev_iphone_sleep_duration", "health_weight_entity": "sensor.dev_iphone_weight",
                 "health_vo2max_entity": "sensor.dev_iphone_vo2_max"}

web = httpx.Client(base_url=INGRESS, follow_redirects=True, timeout=30)
ha_token = (HERE / ".state/ha_token").read_text().strip()
results: list[tuple[bool, str]] = []


def check(ok: bool, name: str, detail: str = "") -> bool:
    results.append((ok, name))
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f"  [{detail}]" if detail and not ok else ""), flush=True)
    return ok


def section(title: str) -> None:
    print(f"\n{title}", flush=True)


def dbq(sql: str, *args, db: str = "/data/training.db") -> list[dict]:
    """Query the add-on's SQLite file (the owner's, or `db`): copied out of the container so we never write to it.
    A copy taken mid-write can be torn ("malformed"); copy again."""
    for attempt in range(5):
        with tempfile.TemporaryDirectory() as d:
            subprocess.run([*COMPOSE, "cp", f"addon:{db}", f"{d}/t.db"], check=True, capture_output=True)
            conn = sqlite3.connect(f"{d}/t.db")
            conn.row_factory = sqlite3.Row
            try:
                return [dict(r) for r in conn.execute(sql, args)]
            except sqlite3.DatabaseError:
                if attempt == 4:
                    raise
            finally:
                conn.close()
        time.sleep(1)


def log_text() -> str:
    return subprocess.run([*COMPOSE, "logs", "--no-log-prefix", "addon"], capture_output=True, text=True).stdout


def tracebacks(since: int = 0) -> list[str]:
    """The exception line that ends each traceback in the add-on log after character offset `since`."""
    out = []
    for block in re.split(r"(?m)^Traceback", log_text()[since:])[1:]:
        final = ""
        for ln in block.splitlines()[1:]:
            if re.match(r"\d{4}-\d\d-\d\d |INFO|WARNING|ERROR", ln):
                break  # the next log line
            if ln and not ln.startswith((" ", "\t", "The above exception", "During handling")):
                final = ln
        out.append(final or block[:80])
    return out


def ha_states() -> dict[str, dict]:
    r = httpx.get(f"{HA}/api/states", headers={"Authorization": f"Bearer {ha_token}"}, timeout=10)
    return {s["entity_id"]: s for s in r.json()}


def wait(cond, what: str, timeout: float = 90, every: float = 1.5):
    end = time.time() + timeout
    while time.time() < end:
        if (v := cond()):
            return v
        time.sleep(every)
    check(False, f"timed out waiting for {what}")
    return None


def page(path: str) -> str:
    r = web.get(path)
    assert r.status_code == 200, f"{path} -> {r.status_code}"
    return r.text


def job_errors(*paths: str) -> list[str]:
    return [m for p in paths or ["/"] for m in re.findall(r"<b>(\w+) failed:</b> ([^<]*)", page(p))]


def post(path: str, **data) -> httpx.Response:
    return web.post(path, data=data)


def pushes() -> list[dict]:
    return httpx.get(f"{SUP}/_dev/push").json()


# --- scenarios --------------------------------------------------------------------------------------------------

def fresh_addon() -> None:
    """Every run starts from an empty add-on: no DB, no log, no settings (what a first install looks like)."""
    print("Resetting the add-on (empty /data volume)...", flush=True)
    subprocess.run([*COMPOSE, "rm", "-sfv", "addon"], check=True, capture_output=True)
    subprocess.run(["docker", "volume", "rm", "-f", "tc-dev_addon-data"], check=True, capture_output=True)
    subprocess.run([*COMPOSE, "up", "-d", "addon"], check=True, capture_output=True)
    for _ in range(40):
        try:
            if web.get("/healthz").status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    sys.exit("The add-on did not come back after the reset")


def reset_fakes() -> None:
    httpx.post(f"{STRAVA}/_dev/reset"), httpx.post(f"{ANTHROPIC}/_dev/reset"), httpx.delete(f"{SUP}/_dev/push")


def startup() -> None:
    section("Startup")
    log = log_text()
    check("Home Assistant API: via Supervisor" in log, "SUPERVISOR_TOKEN survived s6 init (log says 'via Supervisor')")
    check(not tracebacks(), "no tracebacks in the add-on log", "; ".join(tracebacks()))
    check(web.get("/healthz").status_code == 200, "/healthz answers through ingress")
    owner = json.loads((HERE / ".state/users.json").read_text())["dev"]["id"]
    kv = {r["key"]: r["value"] for r in dbq("SELECT key, value FROM kv WHERE key='owner_user_id'")}
    check(json.loads(kv.get("owner_user_id", "null")) == owner and "owns the add-on" in log,
          "the owner is HA's owner, learned over Core's websocket before anyone visits", str(kv))


def pages_and_urls() -> None:
    section("Pages and ingress URLs")
    bad = []
    for p in PAGES:
        r = web.get(p)
        if not check(r.status_code == 200, f"GET {p} -> 200", str(r.status_code)):
            continue
        for url in re.findall(r'\b(?:href|src|action)="(/[^"/][^"]*|/)"', r.text):
            if not url.startswith(PREFIX):
                bad.append(f"{p}: {url}")
    check(not bad, "every absolute href/src/action carries the ingress prefix", "; ".join(bad[:5]))
    check(web.get("/nope").status_code == 404, "unknown path is a plain 404, not a 500")


def settings_and_profile() -> None:
    section("Settings and Profile")
    s = page("/settings")
    check(NOTIFY in s, "Settings lists the Dev iPhone notify service from HA", "notify dropdown missing it")
    r = post("/settings", notify_service=NOTIFY, strava_client_id="12345", strava_client_secret="dev-secret-xyz")
    check(r.status_code == 200 and "Settings saved" in r.text, "saving Settings works", re.findall(r'class="notice[^>]*>([^<]*)', r.text)[:1].__str__())
    check("dev-secret-xyz" not in "".join(page(p) for p in PAGES), "the Strava secret is never rendered back into HTML")
    r = post("/settings", anthropic_api_key="sk-ant-dev-fake-2")
    check("verified" in r.text, "changing the Claude key validates it against the (fake) API", re.findall(r'class="notice[^>]*>([^<]*)', r.text)[:1].__str__())
    check("sk-ant-dev-fake-2" not in page("/settings"), "the Claude key is never rendered back into HTML")
    p = page("/profile")
    check(all(e in p for e in HEALTH_FIELDS.values()), "Profile offers/pre-fills the iPhone's Apple Health sensors")
    r = post("/profile", **HEALTH_FIELDS)
    check(r.status_code == 200 and "Profile saved" in r.text, "saving the health sensors on Profile works")
    rows = wait(lambda: len(dbq("SELECT * FROM health_daily")) >= 5, "the scheduler to read the iPhone sensors (<= 1 min tick)", 120)
    check(bool(rows), "Apple Health values were read from HA into health_daily",
          str(dbq("SELECT metric, value, unit FROM health_daily")))


def strava_sync() -> None:
    section("Strava connect and sync")
    r = web.get(f"{STRAVA}/oauth/authorize", params={"redirect_uri": "http://localhost/strava-callback", "scope": "read,activity:read_all"},
                follow_redirects=False)
    cb = r.headers["location"]
    r = post("/settings/strava/connect", redirect_url=cb)
    check("Connected to Strava as Dev Rider" in r.text, "pasting the callback URL connects Strava", re.findall(r'class="notice[^>]*>([^<]*)', r.text)[:1].__str__())
    for _ in range(8):  # streams are capped per sync run
        wait(lambda: "Syncing" not in page("/"), "sync to finish", 60)
        if not dbq("SELECT 1 FROM activities WHERE streams_fetched=0"):
            break
        post("/sync")
    acts = dbq("SELECT * FROM activities")
    check(len(acts) >= 20, f"rides imported ({len(acts)})")
    check(all(a["streams_fetched"] and a["tss"] is not None and a["ftp_used"] for a in acts), "every ride has streams and is scored (TSS, FTP)")
    check(not job_errors("/"), "no job error banner after sync", str(job_errors("/")))
    for p in [p for p in PAGES if p != "/coach"] + [f"/activity/{acts[-1]['id']}"]:  # /coach: in daily_checkin
        check(web.get(p).status_code == 200, f"GET {p} -> 200 with synced data")


def mqtt_sensors() -> None:
    section("MQTT sensors in HA Core")
    want = ["ftp", "wkg", "ctl", "atl", "tsb", "week_hours", "week_km", "today_session", "last_ride"]
    got = wait(lambda: (lambda s: s if all(f"sensor.training_{k}" in s for k in want) else None)(ha_states()), "sensor.training_* in HA", 30)
    if got:
        bad = [k for k in want if got[f"sensor.training_{k}"]["state"] in ("unknown", "unavailable")]
        check(not bad, "all nine sensor.training_* exist with a value", f"no value: {bad}")
        check(float(got["sensor.training_ftp"]["state"]) > 100, f"FTP sensor is plausible ({got['sensor.training_ftp']['state']} W)")


def stream_404() -> None:
    section("Strava stream 404 does not abort the sync (0.4.1)")
    ride = httpx.post(f"{STRAVA}/_dev/ride", params={"kind": 0, "minutes_ago": 600}).json()
    httpx.post(f"{STRAVA}/_dev/fail", params={"stream_404": ride["id"]})
    post("/sync")
    wait(lambda: ride["id"] in {r["id"] for r in dbq("SELECT id FROM activities")}, "the 404 ride to import", 60)
    time.sleep(2)
    check(not job_errors("/"), "sync finished without a job error", str(job_errors("/")))
    check(web.get("/").status_code == 200, "home renders with an un-streamed ride in the list")
    unscored = dbq("SELECT id FROM activities WHERE id != ? AND (tss IS NULL OR ftp_used IS NULL)", ride["id"])
    check(not unscored, "all other rides stay scored", str(unscored))
    check(ride["id"] in {r["id"] for r in dbq("SELECT id FROM activities")}, "the ride whose stream 404s is imported anyway")


def ride_feedback() -> None:
    section("Ride feedback and push notification")
    httpx.delete(f"{SUP}/_dev/push")
    ride = httpx.post(f"{STRAVA}/_dev/ride", params={"kind": 1, "minutes_ago": 20}).json()
    post("/sync")
    got = wait(lambda: pushes(), "the push notification", 90)
    if got:
        body = json.dumps(got[0])
        url = f"/hassio/ingress/local_training_coach/activity/{ride['id']}"
        check(url in body, "push opens this add-on's ride page (/hassio/ingress/<slug>/activity/<id>)", body[:300])
        check("Solid steady ride" in body, "push carries the coach's headline")
        check('"thread-id": "rides"' in body or "'thread-id': 'rides'" in body or "rides" in body, "push is grouped in the 'rides' thread")
    fb = dbq("SELECT feedback, notified FROM activities WHERE id=?", ride["id"])
    check(bool(fb and fb[0]["feedback"] and fb[0]["notified"]), "feedback stored and ride marked notified")
    n = len(pushes())
    post("/sync")
    time.sleep(8)
    check(len(pushes()) == n, "a second sync sends no second push for the same ride")
    haiku = [r for r in httpx.get(f"{ANTHROPIC}/_dev/requests").json() if r["model"] == "claude-haiku-5-5"]
    check(len(haiku) >= 1, "ride feedback used the Haiku model")
    check("hrv" in json.dumps(haiku[-1]["messages"]) if haiku else False, "Apple Health values reached the feedback prompt")


def daily_checkin() -> None:
    section("Daily check-in and Overview gauges")
    page("/")  # the first page view with rides synced starts it
    rows = wait(lambda: dbq("SELECT * FROM chat_messages WHERE kind='checkin'"), "the daily check-in", 60) or []
    check(len(rows) == 1, "one check-in in the chat after the first page view with rides", str(job_errors("/")))
    page("/plan")
    check(len(dbq("SELECT id FROM chat_messages WHERE kind='checkin'")) == 1, "more page views don't start another one")
    calls = dbq("SELECT model FROM llm_calls WHERE purpose='checkin'")
    check([c["model"] for c in calls] == ["claude-haiku-5-5"], "the check-in runs on Haiku", str(calls))
    check("nav-dot" in page("/"), "the Coach tab shows a dot while the check-in is unread")
    c = page("/coach")
    check("Daily check-in" in c and "Dev fake check-in" in c, "the Coach chat shows the check-in with its label")
    check("nav-dot" not in page("/"), "opening the chat clears the dot")
    home = page("/")
    check('class="gauges"' in home and 'aria-label="Form:' in home, "the Overview status card shows the readiness gauges")


def plan_and_modes() -> None:
    section("Plan generation")
    def plans():
        return dbq("SELECT id, warnings, payload FROM plans ORDER BY id")
    n = len(plans())
    post("/plan")
    wait(lambda: len(plans()) > n, "plan", 90)
    latest = plans()[-1] if len(plans()) > n else None
    check(bool(latest) and not job_errors("/plan"), "Generate plan stores a plan", str(job_errors("/plan")))
    if latest:
        check(json.loads(latest["warnings"] or "[]") == [], "the fake plan passes every validation rule", latest["warnings"])
    check("Endurance spin" in page("/plan") or "Rest" in page("/plan"), "Plan page renders it")

    httpx.post(f"{ANTHROPIC}/_dev/mode", params={"mode": "invalid-plan"})
    calls = len(dbq("SELECT id FROM llm_calls WHERE purpose='plan'"))
    n = len(plans())
    post("/plan")
    wait(lambda: len(plans()) > n, "retried plan", 90)
    check(len(dbq("SELECT id FROM llm_calls WHERE purpose='plan'")) - calls == 2, "an invalid plan is retried exactly once with the errors")
    check(json.loads(plans()[-1]["warnings"] or "[]") == [], "…and the corrected plan is clean")

    httpx.post(f"{ANTHROPIC}/_dev/mode", params={"mode": "invalid-plan-always"})
    n = len(plans())
    post("/plan")
    wait(lambda: len(plans()) > n, "plan with warnings", 90)
    check(json.loads(plans()[-1]["warnings"] or "[]") != [], "a plan that stays invalid is stored with its warnings, not a 500")
    check(web.get("/plan").status_code == 200, "Plan page still renders")
    httpx.post(f"{ANTHROPIC}/_dev/mode", params={"mode": "ok"})
    post("/plan")  # leave a clean active plan behind
    wait(lambda: len(plans()) > n and json.loads(plans()[-1]["warnings"] or "[]") == [], "clean plan", 90)


def chat_and_analysis() -> None:
    section("Coach chat and analysis")
    post("/coach", message="Please make my next open day a rest day, and remember I travel on Mondays")
    msgs = lambda: dbq("SELECT * FROM chat_messages WHERE role='assistant'")
    wait(lambda: msgs(), "the coach's chat reply", 90)
    m = msgs()
    check(bool(m), "the coach replied", str(job_errors("/coach")))
    if m:
        check(bool(m[-1]["remembered"]) and "Dev fact" in m[-1]["remembered"], "a memory fact was saved from the reply")
        if m[-1]["proposal"]:
            post(f"/coach/{m[-1]['id']}/apply")
            check(dbq("SELECT applied_plan_id FROM chat_messages WHERE id=?", m[-1]["id"])[0]["applied_plan_id"] is not None,
                  "applying a proposed plan change creates a new plan version")
        else:
            print("  skip applying a chat proposal: no open dates left in this week's plan (it's the last day of the week)")
    n = len(dbq("SELECT id FROM analyses"))
    post("/analysis", coach="nextlevel")
    wait(lambda: len(dbq("SELECT id FROM analyses")) > n, "analysis", 90)
    check("Analysis (dev fake)" in page("/analysis"), "Analysis page shows the report", str(job_errors("/analysis")))
    reqs = [r for r in httpx.get(f"{ANTHROPIC}/_dev/requests").json() if not (r["output_config"] or {}).get("format")]
    ctx = json.dumps(reqs[-1]["messages"]) if reqs else ""
    check("health" in ctx and "hrv" in ctx and "mean_7d" in ctx, "Apple Health trends reached the analysis snapshot")
    check("Dev fact" in ctx or "athlete_history" in ctx, "athlete memory/history reached the analysis snapshot")
    usage = page("/settings")
    check("claude-opus-5-5" in usage or "Opus" in usage or "opus" in usage, "usage table lists the Claude calls")


def zwift() -> None:
    section("Send to Zwift")
    r = post("/settings/zwift/connect", zwift_username="dev@example.com", zwift_password="pw")
    check("Connected to Zwift as Dev Rider" in r.text, "Zwift connect works", re.findall(r'class="notice[^>]*>([^<]*)', r.text)[:1].__str__())
    plan = dbq("SELECT id, payload FROM plans ORDER BY id DESC LIMIT 1")[0]
    day = next((d for d in json.loads(plan["payload"])["days"] if d["kind"] == "zwift"), None)
    if not day:
        print("  skip Send to Zwift: the latest plan has no Zwift day (single remaining day was a rest day)")
        return
    z = web.get(f"/plan/{plan['id']}/{day['date']}.zwo")
    check(z.status_code == 200 and "<workout_file>" in z.text, ".zwo export is valid XML with a workout_file")
    r = post(f"/plan/{plan['id']}/{day['date']}/zwift")
    files = httpx.get(f"{ZWIFT}/_dev/files").json()
    check(any(k.endswith(".zwo") for k in files) and "workouts.files" in files, "workout and manifest reached the fake Zwift", str(list(files)))


def failure_modes() -> None:
    section("Claude API failures show an error, not a 500")
    for mode in ("429", "500", "refusal"):
        httpx.post(f"{ANTHROPIC}/_dev/mode", params={"mode": mode})
        n = len(dbq("SELECT id FROM analyses"))
        post("/analysis", coach="nextlevel")
        wait(lambda: any(j[0] == "Analysis" for j in job_errors("/analysis")), f"analysis error banner ({mode})", 60)
        errs = [j for j in job_errors("/analysis") if j[0] == "Analysis"]
        check(bool(errs) and len(dbq("SELECT id FROM analyses")) == n, f"mode {mode}: analysis fails with a message", str(errs))
        check(web.get("/").status_code == 200, f"mode {mode}: pages still render")
    httpx.post(f"{ANTHROPIC}/_dev/mode", params={"mode": "ok"})
    section("Strava rate limit")
    httpx.post(f"{STRAVA}/_dev/fail", params={"rate_limit": 1000})
    httpx.post(f"{STRAVA}/_dev/ride", params={"kind": 0, "minutes_ago": 5})
    post("/sync")
    time.sleep(6)
    check(web.get("/").status_code == 200, "a Strava 429 leaves the app serving pages")
    check("rate limit" in page("/").lower() or "Rate limit" in page("/"), "…and says so", "no rate-limit message on home")
    httpx.post(f"{STRAVA}/_dev/fail", params={"rate_limit": 0})


def notice(r: httpx.Response) -> str:
    return str(re.findall(r'class="notice[^>]*>([^<]*)', r.text)[:1])


def multi_tenant() -> None:
    section("A second athlete (dev2, read-only HA user with Dev iPhone 2)")
    users = json.loads((HERE / ".state/users.json").read_text())
    uid = users["dev2"]["id"]
    db2 = f"/data/athletes/{uid}/training.db"
    web2 = httpx.Client(base_url=INGRESS, follow_redirects=True, timeout=30, cookies={"tc_dev_user": "dev2"})
    stranger = httpx.Client(base_url=INGRESS, timeout=30, cookies={"tc_dev_user": "stranger"})
    r = web2.get("/")
    check(r.status_code == 403 and "Hi Dev Two" in r.text, "an HA user the owner hasn't added gets the access page", str(r.status_code))
    check(stranger.get("/settings").status_code == 403, "…on every page")
    s = page("/settings")
    check("Asking for access" in s and "Dev Two" in s, "the owner sees the request on Settings → Athletes")
    check(web2.post(f"/athletes/{uid}/approve").status_code == 403, "dev2 can't approve themselves")
    r = post(f"/athletes/{uid}/approve")
    check("Dev Two can use Training Coach now" in r.text, "the owner approves dev2", notice(r))
    home2 = web2.get("/")
    check(home2.status_code == 200 and "Zwift - Endurance" not in home2.text and not dbq("SELECT id FROM activities", db=db2),
          "dev2 gets an empty add-on of their own, none of the owner's rides")
    s2 = web2.get("/settings").text
    check("Claude (AI coach and analysis)" not in s2 and "Athletes" not in s2, "dev2 sees no Claude key or Athletes card")
    check('value="12345"' not in s2 and "Strava API app, so there" in s2, "dev2 uses the owner's Strava app without seeing it")
    check("notify.mobile_app_dev_iphone_2" in s2 and 'value="notify.mobile_app_dev_iphone"' not in s2,
          "dev2's notify list is only their own phone")
    r = web2.post("/settings", data={"_section": "schedules", "notify_service": NOTIFY})
    check("from your phone" in r.text, "dev2 can't point notifications at the owner's phone", notice(r))
    r = web2.post("/settings", data={"_section": "schedules", "notify_service": NOTIFY + "_2"})
    check("Settings saved" in r.text, "dev2 saves their own phone's notify service", notice(r))
    check("Fill in your Profile" in web2.get("/plan").text, "dev2 is asked for a Profile before the first plan")
    p2 = web2.get("/profile").text
    own = {k: v.replace("dev_iphone_", "dev_iphone_2_") for k, v in HEALTH_FIELDS.items()}
    check(all(e in p2 for e in own.values()) and "sensor.dev_iphone_resting" not in p2,
          "dev2's Apple Health pickers offer only Dev iPhone 2")
    r = web2.post("/profile", data={**own, "health_hrv_entity": HEALTH_FIELDS["health_hrv_entity"], "availability": "1",
                                    "available_days": ["tue", "sat"], "max_weekday_minutes": "45", "max_weekend_minutes": "150"})
    check("from your phone" in r.text, "dev2 can't read the owner's Apple Health sensors", notice(r))
    web2.post("/profile", data=own)
    check("Fill in your Profile" not in web2.get("/plan").text, "after saving the Profile, dev2 can plan")
    rows = wait(lambda: (lambda r: r if len(r) >= 5 else None)(dbq("SELECT metric, value FROM health_daily", db=db2)),
                "dev2's scheduler to read Dev iPhone 2's sensors", 120)
    rhr = {r["metric"]: r["value"] for r in rows or []}.get("rhr")
    check(rhr == 62, f"dev2's health values are their own phone's (resting HR {rhr}, owner's is 52)")
    owner_rides = len(dbq("SELECT id FROM activities"))
    r = web2.get(f"{STRAVA}/oauth/authorize", params={"redirect_uri": "http://localhost/strava-callback",
                                                     "scope": "read,activity:read_all"}, follow_redirects=False)
    r = web2.post("/settings/strava/connect", data={"redirect_url": r.headers["location"]})
    check("Connected to Strava" in r.text, "dev2 connects their own Strava account", notice(r))
    wait(lambda: len(dbq("SELECT id FROM activities", db=db2)) >= 20, "dev2's first sync", 90)
    check(len(dbq("SELECT id FROM activities")) == owner_rides, "dev2's sync leaves the owner's rides alone")
    check(bool(dbq("SELECT 1 FROM kv WHERE key='strava_token'", db=db2)), "dev2's Strava token is in dev2's own DB")
    httpx.delete(f"{SUP}/_dev/push")
    httpx.post(f"{STRAVA}/_dev/ride", params={"kind": 2, "minutes_ago": 15})
    web2.post("/sync")
    got = wait(lambda: [p for p in pushes() if "dev-push-token-2" in json.dumps(p)], "a ride push to Dev iPhone 2", 90)
    check(bool(got), "dev2's ride feedback goes to Dev iPhone 2")
    sensors = [e for e in ha_states() if e.startswith("sensor.training_")]
    check(len(sensors) == 9, f"no extra MQTT sensors for dev2 ({len(sensors)} sensor.training_*)")
    r = post(f"/athletes/{uid}/remove")
    check(web2.get("/").status_code == 403 and "no longer has access" in r.text, "the owner can remove dev2 again", notice(r))


def mobile() -> None:
    section("iPhone layout (tools/mobile_check.py)")
    r = subprocess.run([str(HERE.parent / ".venv/bin/python"), str(HERE.parent / "tools/mobile_check.py")],
                       env={**os.environ, "TC_URL": INGRESS}, capture_output=True, text=True)
    check(r.returncode == 0, "no horizontal overflow at 390 px on any page", (r.stdout + r.stderr)[-400:])


def main() -> None:
    try:
        httpx.get(f"{SUP}/_dev/push", timeout=3).raise_for_status()
        web.get("/healthz").raise_for_status()
    except httpx.HTTPError:
        sys.exit("The dev stack isn't up. Run dev/devha.sh up first.")
    if "--keep" not in sys.argv:
        fresh_addon()
    reset_fakes()
    startup()
    pages_and_urls()
    settings_and_profile()
    strava_sync()
    daily_checkin()  # before any section opens /coach, which marks it read
    mqtt_sensors()
    stream_404()
    ride_feedback()
    plan_and_modes()
    chat_and_analysis()
    zwift()
    multi_tenant()
    tb = tracebacks()
    check(not tb, "no unexpected tracebacks during the happy paths", "; ".join(tb[:3]))
    failure_modes()
    expected = [t for t in tracebacks() if not t.startswith(("app.llm.LLMError", "anthropic.", "app.strava.RateLimited"))]
    check(not [t for t in expected if t not in tb], "failure-mode tracebacks are only the expected LLMError/RateLimited", "; ".join(expected[:3]))
    if "--mobile" in sys.argv:
        mobile()
    failed = [n for ok, n in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed" + (f"; FAILED: {failed}" if failed else ""))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
