import copy

from app import coach, coaches
from app.config import Config
from app.db import DB

from .test_workouts import step

CFG = Config(available_days=["mon", "tue", "wed", "thu", "sat", "sun"], max_weekday_minutes=75, max_weekend_minutes=180)
DATES = [f"2026-10-{d}" for d in range(12, 19)]  # Mon 12 → Sun 18


def day(date, kind="rest", minutes=0, intensity="recovery", tss=0, steps=None):
    return {"date": date, "kind": kind, "title": "x", "description": "", "duration_min": minutes,
            "intensity": intensity, "target_tss": tss, "bike": "none", "terrain": "",
            "steps": steps or []}


def sweetspot(minutes=60):
    return [step("warmup", duration_s=600, power_low=0.5, power_high=0.75),
            step("steady", duration_s=(minutes - 20) * 60, power_low=0.9, power_high=0.9),
            step("cooldown", duration_s=600, power_low=0.6, power_high=0.4)]


def good_plan():
    return {"week_focus": "", "rationale": "", "days": [
        day(DATES[0], "zwift", 60, "sweetspot", 0, sweetspot(60)),
        day(DATES[1], "zwift", 60, "endurance", 0, [step("steady", duration_s=3600, power_low=0.65, power_high=0.65)]),
        day(DATES[2], "zwift", 60, "threshold", 0, sweetspot(60)),
        day(DATES[3], "zwift", 45, "recovery", 0, [step("steady", duration_s=2700, power_low=0.5, power_high=0.5)]),
        day(DATES[4]),
        day(DATES[5], "outdoor", 150, "endurance", 120),
        day(DATES[6], "outdoor", 120, "endurance", 90),
    ]}


def run(plan, **kw):
    args = dict(dates=DATES, cfg=CFG, ctl=50, done_tss=0, recovery=False)
    args.update(kw)
    return coach.validate(plan, **args)


def test_good_plan_passes_and_normalises_zwift_tss():
    p = good_plan()
    assert run(p) == []
    assert p["days"][0]["target_tss"] > 50


def test_wrong_dates():
    p = good_plan()
    p["days"] = p["days"][1:]
    assert "exactly these dates" in run(p)[0]


def test_unavailable_day_and_too_long():
    p = good_plan()
    p["days"][4] = day(DATES[4], "zwift", 30, "endurance", 0, [step("steady", duration_s=1800, power_low=0.6, power_high=0.6)])
    p["days"][5]["duration_min"] = 240
    errs = run(p)
    assert any("2026-10-16" in e and "availability 0" in e for e in errs)
    assert any("2026-10-17" in e and "240 min" in e for e in errs)


def test_back_to_back_hard_days():
    p = good_plan()
    p["days"][1] = copy.deepcopy(p["days"][2])
    p["days"][1]["date"] = DATES[1]
    assert any("2 hard days in a row" in e for e in run(p))


def test_steps_must_match_duration():
    p = good_plan()
    p["days"][0]["duration_min"] = 90
    assert any("steps sum to 60 min" in e for e in run(p))


def test_weekly_tss_cap_and_recovery():
    p = good_plan()
    assert any("exceeds cap" in e for e in run(p, ctl=10, done_tss=200))
    assert any("recovery week" in e for e in run(good_plan(), recovery=True, ctl=30))


def test_block_week_cycles(tmp_path):
    from datetime import date, timedelta
    db = DB(tmp_path / "t.db")
    start = date(2026, 10, 5)
    assert coach.block_week(db, start) == 1
    assert [coach.block_week(db, start + timedelta(weeks=i)) for i in range(5)] == [1, 2, 3, 4, 1]


def test_compliance():
    planned = day(DATES[0], "zwift", 60)
    assert coach.compliance(planned, []) == "missed"
    assert coach.compliance(planned, [{"moving_time": 3500}]) == "done"
    assert coach.compliance(planned, [{"moving_time": 1200}]) == "partial"
    assert coach.compliance(day(DATES[4]), [{"moving_time": 1200}]) == "extra"


def threshold(minutes=60):
    return sweetspot(minutes)  # 40 min at 90% FTP: hard by content, whatever it's labelled


def hard_days(*idx, label="endurance"):
    """good_plan with hard workouts on the given day indexes and easy ones on the other weekdays."""
    p = good_plan()
    for i in range(4):
        p["days"][i] = day(DATES[i], "zwift", 60, "endurance", 0, [step("steady", duration_s=3600, power_low=0.65, power_high=0.65)])
    for i in idx:
        p["days"][i] = day(DATES[i], "zwift", 60, label, 0, threshold())
    return p


def rules(key, level="intermediate", weeks=0):
    return coaches.get(key).rules(level, weeks)


def test_hard_is_judged_from_content_not_label():
    assert any("2 hard days in a row" in e for e in run(hard_days(0, 1, label="sweetspot")))
    assert run(hard_days(0, 2, label="sweetspot")) == []


def test_carmichael_allows_pairs_but_not_three_in_a_row():
    car = rules("carmichael", "advanced")
    assert run(hard_days(0, 1), rules=car) == []
    assert any("3 hard days in a row ending 2026-10-14" in e for e in run(hard_days(0, 1, 2), rules=car))


def test_hard_session_cap_depends_on_level_and_time_with_coach():
    p = hard_days(0, 2, 3)  # Wed+Thu pair
    assert any("3 hard days this week" in e for e in run(p))  # default coach: 2
    assert run(p, rules=rules("carmichael", "intermediate")) == []
    assert any("at most 2" in e for e in run(p, rules=rules("carmichael", "beginner")))
    assert any("at most 2" in e for e in run(p, rules=rules("carmichael", "advanced", weeks=8)))
    assert rules("pogacar", "beginner")["max_hard_sessions"] == 1


def test_recovery_week_allows_one_hard_day_and_no_vo2max():
    car = rules("carmichael", "advanced")
    errs = run(hard_days(0, 2), rules=car, recovery=True)
    assert any("at most 1 in a recovery week" in e for e in errs)
    p = hard_days(0)
    p["days"][2]["intensity"] = "vo2max"
    assert any("no vo2max in a recovery week" in e for e in run(p, rules=car, recovery=True))
    # Openers: 4 × 1 min above 105% FTP is under the 5-min bar, so it isn't a hard day
    openers = [step("warmup", duration_s=900, power_low=0.5, power_high=0.7),
               step("intervals", repeat=4, on_s=60, off_s=120, on_power=1.2, off_power=0.5),
               step("steady", duration_s=1620, power_low=0.6, power_high=0.6),
               step("cooldown", duration_s=300, power_low=0.6, power_high=0.4)]
    p = hard_days(0)
    p["days"][2] = day(DATES[2], "zwift", 60, "endurance", 0, openers)
    assert [e for e in run(p, recovery=True) if not e.startswith("week TSS")] == []


def test_rides_already_done_count():
    p = hard_days(2)
    p["days"] = p["days"][2:]  # planning from Wednesday
    dates = DATES[2:]
    yesterday_hard = [{"date": DATES[1], "if": 0.9}]
    assert any("2 hard days in a row ending 2026-10-14" in e
               for e in run(p, dates=dates, done_rides=yesterday_hard))
    p = hard_days(4, 6)
    p["days"] = p["days"][4:]
    p["days"][0]["duration_min"] = 0  # Friday is unavailable; keep it a rest day
    p["days"][0] = day(DATES[4])
    p["days"][2] = day(DATES[6], "zwift", 60, "endurance", 0, threshold())
    done = [{"date": DATES[0], "if": 0.9}, {"date": DATES[2], "if": 0.88}]
    assert any("3 hard days this week" in e for e in run(p, dates=DATES[4:], done_rides=done))


def test_system_prompt_carries_philosophy_and_style_rules():
    pog, car = coaches.get("pogacar"), coaches.get("carmichael")
    assert "San Millán" in coach.system_prompt(pog, pog.rules())
    assert "Never two hard days in a row" in coach.system_prompt(pog, pog.rules())
    car_prompt = coach.system_prompt(car, car.rules("advanced"))
    assert "time-crunched" in car_prompt and "At most 2 hard days in a row" in car_prompt
    assert "At most 4 hard sessions" in car_prompt


def test_weeks_with_coach(tmp_path):
    from datetime import date
    db = DB(tmp_path / "t.db")
    for ws in ("2026-09-28", "2026-10-05"):
        db.save_plan(ws, "t", "m", {"days": []}, [], "carmichael")
    assert coach.weeks_with_coach(db, "carmichael", date(2026, 10, 12)) == 2
    assert coach.weeks_with_coach(db, "pogacar", date(2026, 10, 12)) == 0


def test_unknown_coach_falls_back_and_names():
    assert coaches.get("nope").key == "nextlevel"
    assert coaches.name_of(None) == "Original coach"
    assert all(c.philosophy.startswith("#") for c in coaches.COACHES.values())


def test_active_plan_and_week_plans(tmp_path):
    db = DB(tmp_path / "t.db")
    first = db.save_plan("2026-10-12", "t1", "m", {"days": []}, [], "pogacar")
    second = db.save_plan("2026-10-12", "t2", "m", {"days": []}, [], "carmichael")
    newer_pog = db.save_plan("2026-10-12", "t3", "m", {"days": []}, [], "pogacar")
    assert db.active_plan("2026-10-12")["id"] == first  # first plan of the week stays active
    assert {k: p["id"] for k, p in db.week_plans("2026-10-12").items()} == {"pogacar": newer_pog, "carmichael": second}
    db.set_active_plan("2026-10-12", second)
    assert db.active_plan("2026-10-12")["coach"] == "carmichael"


def test_migration_adds_coach_columns(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript("CREATE TABLE plans (id INTEGER PRIMARY KEY AUTOINCREMENT, week_start TEXT, created_at TEXT,"
                      " model TEXT, payload TEXT, warnings TEXT);"
                      "CREATE TABLE analyses (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, kind TEXT,"
                      " model TEXT, markdown TEXT);"
                      "INSERT INTO plans(week_start, payload, warnings) VALUES('2026-10-05', '{}', '[]');")
    con.commit()
    con.close()
    db = DB(path)
    old = db.latest_plan("2026-10-05")
    assert old["coach"] is None and db.active_plan("2026-10-05")["id"] == old["id"]
    db.save_analysis("t", "manual", "m", "x", "pogacar")
    assert db.analysis()["coach"] == "pogacar"
