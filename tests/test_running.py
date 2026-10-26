"""0.11.0: runs and walks, run load, the running coaches, two-sport planning and the run durability guard."""
import json
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from app import chat, checkin, coach, coaches, llm, memory, metrics, sports, stats
from app.config import Config, load_config, save_config
from app.db import DB
from app.sync import recompute

from .test_chat import client  # noqa: F401 - the fixture
from .test_coach import CFG, DATES, day, good_plan
from .test_coach import run as validate

WS = date(2026, 10, 12)


def run_day(date_, minutes=40, intensity="endurance", tss=40):
    return {"date": date_, "kind": "run", "title": "Run", "description": "", "duration_min": minutes,
            "intensity": intensity, "target_tss": tss, "target_pace": "5:30/km", "surface": "road", "sport": "running",
            "coach": "runnextlevel", "steps": [], "bike": "none", "terrain": "", "test_protocol": "none"}


def rest_run(date_):
    return run_day(date_, 0, "recovery", 0) | {"kind": "rest", "target_pace": "", "surface": "none"}


def history(cap=220, longest=70, returning=False, max_hard=2):
    """snapshot.running's limits (stats.running works them out)."""
    return {"cap_minutes": cap, "longest_run_cap_min": longest, "returning": returning, "max_hard_runs": max_hard}


def activity(i, sport, day_, minutes=40, hr=150, speed=3.0):
    return {"id": i, "name": f"{sport} {i}", "sport_type": sport, "start_date": f"{day_}T06:00:00Z",
            "start_date_local": f"{day_}T07:00:00", "distance": minutes * 60 * speed, "moving_time": minutes * 60,
            "elapsed_time": minutes * 60, "elevation_gain": 50, "trainer": 0, "avg_watts": 250, "weighted_avg_watts": 260,
            "max_watts": 400, "device_watts": 1, "avg_hr": hr, "max_hr": hr and hr + 15, "kilojoules": None, "avg_speed": speed}


# --- load ---------------------------------------------------------------------------------------------------------

def test_rtss_and_threshold_pace():
    tss, intensity = metrics.tss_from_pace(3600, 1000 / 300, 300)  # an hour at threshold pace
    assert (round(tss), intensity) == (100, pytest.approx(1.0))
    assert metrics.tss_from_pace(3600, 1000 / 360, 300)[1] == pytest.approx(300 / 360)
    # Runs of 20-60 min scaled to an hour (Riegel): a 30-min run at 4:50/km says ~5:02/km threshold. Too short, too
    # long and walking speed don't count
    assert metrics.threshold_pace([(1800, 1000 / 290), (2400, 1000 / 320), (600, 5.0), (7200, 4.0), (1800, 1.2)]) == 302
    assert metrics.threshold_pace([(3600, 1000 / 300)]) == 300  # an hour is threshold as it is
    assert metrics.threshold_pace([(600, 5.0)]) is None


def test_run_load_hr_first_then_pace_then_estimate(tmp_path):
    db = DB(tmp_path / "t.db")
    db.upsert_activity_summary(activity(1, "Run", "2026-10-10", hr=160))
    db.upsert_activity_summary(activity(2, "Run", "2026-10-11", hr=None, speed=1000 / 300) | {"avg_hr": None})
    db.upsert_activity_summary(activity(3, "TrailRun", "2026-10-12") | {"avg_hr": None, "avg_speed": None})
    db.upsert_activity_summary(activity(4, "Walk", "2026-10-12", minutes=60, hr=100))
    db.upsert_activity_summary(activity(5, "Ride", "2026-10-12", minutes=60))
    recompute(db, Config(lthr=160, run_lthr=170, threshold_pace=300))
    hr_run, pace_run, bare, walk, ride = (db.activity(i) for i in range(1, 6))
    assert hr_run["tss_source"] == "rhr" and hr_run["tss"] == pytest.approx(40 / 60 * (160 / 170) ** 2 * 100, abs=0.1)
    assert pace_run["tss_source"] == "rpace" and pace_run["tss"] == pytest.approx(40 / 60 * 100, abs=0.1)
    assert bare["tss_source"] == "estimate" and bare["tss"] == pytest.approx(40 / 60 * 55, abs=0.1)
    assert walk["tss_source"] == "estimate" and walk["tss"] == pytest.approx(20)  # a flat 20/h, HR or not
    assert ride["tss_source"] == "power" and ride["ftp_used"] and not hr_run["ftp_used"]  # runs never get an FTP
    assert Config(lthr=160).run_threshold_hr == 165  # no running LTHR set: cycling's + 5


def test_running_limits_come_from_running_history():
    today = date(2026, 10, 28)  # a Wednesday
    acts = [activity(i, "Run", d, minutes) for i, (d, minutes) in enumerate(
        [("2026-09-29", 40), ("2026-10-06", 50), ("2026-10-13", 45), ("2026-10-20", 65), ("2026-10-26", 30)])]
    r = stats.running(acts, today, Config(running_coach="almgren", athlete_level="elite"))
    assert r["avg_minutes_last_4_weeks"] == 50 and r["cap_minutes"] == 90  # the floor: they ran in the last 12 weeks
    assert (r["longest_run_min_30d"], r["longest_run_cap_min"]) == (65, 72)
    assert r["max_hard_runs"] == 2 and not r["returning"]  # 3 only at ≥75 km a week
    assert stats.running([], today, Config())["cap_minutes"] == 60  # nothing run in 12 weeks: a gentler floor
    weeks = lambda *n: [{"runs": x} for x in n]  # noqa: E731 - completed weeks oldest first, then the current one
    assert stats.returning(weeks(1, 1, 0, 0, 0, 0, 0, 0)) and stats.returning(weeks(1, 0, 0, 0, 0, 2, 0))
    assert not stats.returning(weeks(1, 1, 1, 1, 1, 1, 1, 0))


def test_ride_views_leave_runs_out(tmp_path):
    acts = [activity(1, "Ride", "2026-10-10", 60) | {"tss": 60, "mmp": {1200: 250}, "trainer": 0},
            activity(2, "Run", "2026-10-11", 40) | {"tss": 40, "mmp": {1200: 400}, "trainer": 0},
            activity(3, "Walk", "2026-10-11", 60, speed=1.4) | {"tss": 10, "trainer": 0}]
    w = stats.window(acts, date(2026, 10, 5))
    assert (w["rides"], w["hours"], w["km"], w["tss"]) == (1, 1.0, 11, 110)  # load counts everything
    assert (w["runs"], w["run_hours"], w["walk_hours"]) == (1, 0.7, 1.0)
    assert stats.power_curve(acts, date(2026, 10, 12), 90) == {1200: 250}  # a Stryd number never reaches the curve


def test_walk_counts_for_a_recovery_session_only():
    long_walk = {"sport_type": "Walk", "moving_time": 60 * 60}
    assert coach.compliance(day(DATES[0], "zwift", 45, "recovery"), [long_walk]) == "done"
    assert coach.compliance(run_day(DATES[0], 30, "recovery"), [long_walk]) == "done"
    assert coach.compliance(day(DATES[0], "outdoor", 60, "endurance"), [long_walk]) == "missed"  # not an endurance ride
    assert coach.compliance(run_day(DATES[0], 40), [long_walk]) == "missed"  # nor an easy run's impact loading
    assert coach.compliance(day(DATES[0], "zwift", 45, "threshold"), [long_walk]) == "missed"
    assert coach.compliance(day(DATES[0], "zwift", 45, "recovery"), [long_walk | {"moving_time": 1800}]) == "missed"
    assert coach.compliance(day(DATES[0]), [long_walk]) == "rest"  # a walk on a rest day isn't an unplanned session
    assert coach.compliance(day(DATES[0], "zwift", 45), [{"sport_type": "Run", "moving_time": 2700}]) == "missed"
    assert coach.compliance(run_day(DATES[0], 40), [{"sport_type": "Run", "moving_time": 2400}]) == "done"


# --- config and coaches ------------------------------------------------------------------------------------------

def test_default_coach_migrates_and_until_puts_a_sport_back_on_hold(tmp_path):
    db = DB(tmp_path / "t.db")
    db.set("settings", {"default_coach": "carmichael"})
    cfg = load_config(db)
    assert cfg.cycling_coach == "carmichael" and cfg.active_sports(date(2026, 10, 12)) == ["cycling"]
    cfg.running_active, cfg.running_until, cfg.cycling_active = True, "2026-10-14", False
    save_config(db, cfg)
    cfg = load_config(db)
    assert cfg.active_sports(date(2026, 10, 14)) == ["running"]
    assert cfg.active_sports(date(2026, 10, 15)) == ["cycling"]  # both on hold: the primary sport is back
    cfg.cycling_active = True
    assert cfg.active_sports(date(2026, 10, 13)) == ["cycling", "running"]
    cfg.primary_sport = "running"
    assert cfg.active_sports(date(2026, 10, 13)) == ["running", "cycling"]


def test_running_coaches():
    assert {c.key for c in coaches.for_sport("running")} == {"almgren", "runnextlevel"}
    assert coaches.get("pogacar", "running").key == "runnextlevel"  # a cycling key never coaches running
    assert coaches.get("almgren").rules("advanced")["max_hard_sessions"] == 3
    assert coaches.get("runnextlevel").rules("beginner")["max_hard_sessions"] == 1
    prompt = coach.system_prompt(coaches.get("almgren"), coaches.get("almgren").rules(), "second")
    assert "75 km" in prompt and "longest_run_cap_min" in prompt and "other_sport_days" in prompt and "Zwift" not in prompt


def test_profile_sports_card(client):  # noqa: F811
    from app import main
    st = main.app.state.st
    r = client.post("/profile", data={"_section": "sports", "cycling_coach": "nextlevel", "running_coach": "almgren",
                                      "running_active": "on", "running_until": "2026-11-08", "primary_sport": "cycling",
                                      "threshold_pace": "4:45", "run_lthr": "172"})
    assert r.status_code == 200
    cfg = load_config(st.db)
    assert (cfg.cycling_active, cfg.running_active, cfg.running_until, cfg.running_coach) == (False, True, "2026-11-08", "almgren")
    assert (cfg.threshold_pace, cfg.run_lthr) == (285, 172)
    assert "Running coach: Next Level Run → Almgren (double threshold)" in client.get("/profile").text
    client.post("/profile", data={"_section": "sports", "threshold_pace": "fast", "running_until": "soon"})
    cfg = load_config(st.db)
    assert cfg.running_active  # nothing ticked: both would be on hold, so the change is refused
    assert cfg.threshold_pace == 285 and cfg.running_until == "2026-11-08"
    client.post("/profile", data={"running_coach": "pogacar"})
    assert load_config(st.db).running_coach == "almgren"
    client.post("/profile", data={"_section": "sports", "running_active": "on", "threshold_pace": "4.50", "run_lthr": "-150"})
    cfg = load_config(st.db)
    assert cfg.threshold_pace == 290 and cfg.run_lthr == 172  # the iPhone's decimal keypad works; -150 bpm doesn't


async def test_a_db_from_before_lists_the_whole_year_once(tmp_path):
    from app.sync import sync

    class Source:
        def __init__(self):
            self.after = []

        async def activities_since(self, after_epoch):
            self.after.append(after_epoch)
            return []

        @staticmethod
        def summary(a):
            return a

        async def streams(self, activity_id):
            return {}

    db = DB(tmp_path / "t.db")
    db.set("last_activity_start", "2026-10-20T06:00:00Z")  # a 0.10.x DB: rides only so far
    src = Source()
    await sync(db, src, Config())
    await sync(db, src, Config())
    year, resync = (datetime.now().timestamp() - a for a in src.after)
    assert year > 360 * 86400 and resync < 30 * 86400  # then back to the 14-day re-list


# --- the combined validator ----------------------------------------------------------------------------------------

def test_a_hard_run_the_day_after_bike_intervals_is_back_to_back():
    p = {"days": [*map(rest_run, DATES[:3]), run_day(DATES[3], 40, "vo2max"), *map(rest_run, DATES[4:])]}  # Wed: bike threshold
    bike = [d | {"sport": "cycling"} for d in good_plan()["days"] if d["kind"] != "rest"]
    errs = validate(p, rules=coaches.get("runnextlevel").rules(), fixed=bike, running=history(200))
    assert any("hard days in a row" in e for e in errs) and any("hard days this week" in e for e in errs)


def test_both_sessions_must_fit_the_day():
    p = {"days": [run_day(DATES[0], 30), *map(rest_run, DATES[1:])]}
    bike = [d | {"sport": "cycling"} for d in good_plan()["days"] if d["date"] == DATES[0]]  # 60 min Zwift
    assert any("90 min (both sports together) exceeds availability 75" in e for e in validate(p, fixed=bike, running=history(200)))
    p["days"][0]["duration_min"] = 15
    assert not [e for e in validate(p, fixed=bike, running=history(200)) if "availability" in e]


def test_run_durability_caps_minutes_from_running_history_not_ctl():
    p = {"days": [run_day(DATES[0], 40), rest_run(DATES[1]), run_day(DATES[2], 40), rest_run(DATES[3]),
                  run_day(DATES[4], 40), rest_run(DATES[5]), run_day(DATES[6], 60)]}
    cap = [e for e in validate(p, ctl=90, running=history(90)) if "durability" in e]
    assert cap and "180 run minutes" in cap[0] and "cap of 90" in cap[0]  # a CTL of 90 changes nothing
    assert not [e for e in validate(p, running=history(187)) if "durability" in e]
    # Runs already done this week count too, but not last Sunday's, which only counts for back-to-back days
    this_week = [{"date": DATES[0], "sport": "run", "minutes": 30, "if": 0.8}]
    p["days"] = p["days"][1:]
    assert [e for e in validate(p, dates=DATES[1:], running=history(165), done_rides=this_week) if "durability" in e]
    sunday = [{"date": "2026-10-11", "sport": "run", "minutes": 60, "if": 0.8}]
    assert not [e for e in validate(p, dates=DATES[1:], running=history(165), done_rides=sunday) if "durability" in e]


def test_no_single_run_much_longer_than_the_longest_lately():
    p = {"days": [*map(rest_run, DATES[:6]), run_day(DATES[6], 90)]}
    errs = [e for e in validate(p, running=history(longest=66)) if "longer than" in e]
    assert errs and "90 min run is longer than 66 min" in errs[0]


def test_no_hard_run_in_the_first_two_weeks_back_and_a_hard_run_cap():
    p = {"days": [run_day(DATES[0], 30, "hills"), *map(rest_run, DATES[1:])]}
    assert any("none in the first 2 weeks back" in e for e in validate(p, running=history(returning=True)))
    p["days"][0]["intensity"] = "endurance"  # strides in an easy run are fine
    assert not validate(p, running=history(returning=True))
    p = {"days": [run_day(DATES[0], 30, "threshold"), rest_run(DATES[1]), run_day(DATES[2], 30, "hills"), rest_run(DATES[3]),
                  run_day(DATES[4], 30, "vo2max"), *map(rest_run, DATES[5:])]}
    errs = validate(p, rules=coaches.get("almgren").rules("elite"), running=history(max_hard=2))
    assert "3 hard runs this week; at most 2" in errs
    assert not [e for e in validate(p, rules=coaches.get("almgren").rules("elite"), running=history(max_hard=3)) if "hard runs" in e]


def test_no_hills_in_a_recovery_week():
    p = {"days": [run_day(DATES[0], 30, "hills"), *map(rest_run, DATES[1:])]}
    assert any("no hills in a recovery week" in e for e in validate(p, recovery=True, running=history()))


def test_a_run_scored_on_pace_isnt_judged_hard():
    assert coach.done_hard({"sport": "run", "if": 0.95, "tss_source": "rhr"})
    assert not coach.done_hard({"sport": "run", "if": 0.95, "tss_source": "rpace"})
    assert not coach.done_hard({"sport": "walk", "if": 1.2})


# --- two-pass planning -------------------------------------------------------------------------------------------

async def test_two_sports_plan_primary_first_then_the_other_around_it(tmp_path, monkeypatch):
    db = DB(tmp_path / "t.db")
    runs = {"week_focus": "Easy running", "rationale": "Legs first.",
            "days": [rest_run(d) | {"title": "Rest"} for d in DATES]}
    for k in ("sport", "coach", "steps", "bike", "terrain", "test_protocol"):  # what the model returns
        for d in runs["days"]:
            d.pop(k)
    runs["days"][1] = {k: v for k, v in run_day(DATES[1], 40).items() if k in coach.RUN_DAY["properties"]}
    calls = []

    async def fake_run(api_key, system, messages, **kw):
        calls.append((system, messages[-1]["content"], kw["schema"]))
        reply = good_plan() if len(calls) == 1 else runs
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(reply))])

    monkeypatch.setattr(llm, "run", fake_run)
    cfg = Config(anthropic_api_key="k", running_active=True, available_days=CFG.available_days, max_weekday_minutes=120)
    snapshot = {"recent_rides": [], "load": {"ctl": 50}, "running": history(100)}
    plan = db.plan(await coach.generate_plan(db, cfg, snapshot, WS, date(2026, 10, 11)))

    assert len(calls) == 2
    assert "cycling coach" in calls[0][0] and "You are the primary coach" in calls[0][0]
    assert "running coach" in calls[1][0] and "other_sport_days" in calls[1][1] and '"tss_budget_left"' in calls[1][1]
    assert calls[1][2]["properties"]["days"]["items"] is coach.RUN_DAY
    days = plan["payload"]["days"]
    assert [(d["date"], d["sport"]) for d in days if d["date"] == DATES[1]] == [(DATES[1], "cycling"), (DATES[1], "running")]
    assert [d for d in days if d["date"] == DATES[4]] == [d for d in days if d["date"] == DATES[4] and d["kind"] == "rest"][:1]
    assert plan["coach"] == "nextlevel" and plan["payload"]["sports"]["running"]["coach"] == "runnextlevel"
    assert memory.plan_coach(plan, "running") == "runnextlevel" and memory.plan_coach(plan, "cycling") == "nextlevel"


async def test_one_sport_on_hold_plans_one_pass(tmp_path, monkeypatch):
    calls = []

    async def fake_run(api_key, system, messages, **kw):
        calls.append(system)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(good_plan()))])

    monkeypatch.setattr(llm, "run", fake_run)
    db = DB(tmp_path / "t.db")
    await coach.generate_plan(db, Config(anthropic_api_key="k"), {"recent_rides": [], "load": {"ctl": 50}}, WS, date(2026, 10, 11))
    assert len(calls) == 1 and "They train two sports" not in calls[0]


# --- chat and check-in ---------------------------------------------------------------------------------------------

def two_sport_plan(db):
    days = [d | {"sport": "cycling", "coach": "nextlevel"} for d in good_plan()["days"]]
    days = [d for d in days if d["date"] not in DATES[3:5]] + [run_day(DATES[4], 30), run_day(DATES[1], 20),
                                                                 day(DATES[3]) | {"sport": "cycling", "coach": "nextlevel"}]
    days.sort(key=lambda d: (d["date"], d["sport"] != "cycling"))
    return db.save_plan(WS.isoformat(), "2026-10-11T20:00:00", "m", {"week_focus": "", "rationale": "", "block_week": 1,
                                                                     "days": days}, [], "nextlevel")


def test_chat_goes_to_the_coach_picked_and_changes_only_their_sport(tmp_path):
    db = DB(tmp_path / "t.db")
    cfg = Config(running_active=True)
    today = date(2026, 10, 13)
    pid = two_sport_plan(db)
    assert [c.key for c in chat.chat_coaches(db, cfg, today)] == ["nextlevel", "runnextlevel"]
    assert chat.addressed(db, cfg, today, "runnextlevel").key == "runnextlevel"
    assert chat.addressed(db, cfg, today, "pogacar").key == "nextlevel"  # not on the team: the primary sport's coach
    plan = db.plan(pid)
    # The running coach drops Friday's run: the ride days stay, Friday becomes a rest day
    msg = db.add_chat("2026-10-13T10:00:00", "assistant", "runnextlevel", "Rest Friday.",
                      {"plan_id": pid, "sport": "running", "days": [rest_run(DATES[4])]})
    new_id, _ = chat.apply(db, db.chat_message(msg), today)
    days = db.plan(new_id)["payload"]["days"]
    assert [(d["sport"], d["kind"]) for d in days if d["date"] == DATES[4]] == [("running", "rest")]
    assert len([d for d in days if d["date"] == DATES[1]]) == 2  # Tuesday's ride and run untouched
    # The validator sees the other sport's sessions as fixed
    own = chat.own_days(plan, DATES[1:], "running")
    assert [d["kind"] for d in own] == ["run", "rest", "rest", "run", "rest", "rest"]
    assert [d["kind"] for d in chat.own_days(plan, DATES[1:], "cycling")] == ["zwift", "zwift", "rest", "rest", "outdoor", "outdoor"]
    assert all(d["sport"] == "cycling" for d in chat.others(plan, DATES[1:], "running"))


def test_checkin_comes_from_the_coach_of_the_day(tmp_path):
    db = DB(tmp_path / "t.db")
    cfg = Config(running_active=True)
    two_sport_plan(db)
    assert checkin.writer(db, cfg, date(2026, 10, 16)).key == "runnextlevel"  # Thu rest, Fri a run
    assert checkin.writer(db, cfg, date(2026, 10, 13)).key == "nextlevel"     # Tue: ride and run → the primary coach
    block = checkin.today_block(db, [], date(2026, 10, 13), "cycling")
    assert block["session"]["sport"] == "cycling" and block["also_today"]["sport"] == "running"


def test_walks_are_silent(tmp_path, monkeypatch):
    from app import feedback, ha
    db = DB(tmp_path / "t.db")
    now = datetime(2026, 10, 12, 12, 0)
    db.upsert_activity_summary(activity(1, "Walk", "2026-10-12", 60) | {"start_date": "2026-10-12T08:00:00Z"})
    sent = []

    async def notify(*a, **k):
        sent.append(a)
    monkeypatch.setattr(ha, "notify", notify)
    import asyncio
    from datetime import timezone
    asyncio.run(feedback.after_sync(db, Config(anthropic_api_key="k"), now.replace(tzinfo=timezone.utc)))
    assert sent == [] and db.activity(1)["notified"] == 1


def test_sport_kinds():
    assert [sports.kind(t) for t in ("VirtualRide", "TrailRun", "Hike", "Yoga", None)] == ["ride", "run", "walk", None, None]
    assert sports.day_key({"date": "2026-10-12"}) == "2026-10-12/cycling"
