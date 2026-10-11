import copy

from app import coach
from app.config import Config
from app.db import DB

from .test_workouts import step

CFG = Config(available_days=["mon", "tue", "wed", "thu", "sat", "sun"], max_weekday_minutes=75, max_weekend_minutes=180)
DATES = [f"2026-10-{d}" for d in range(12, 19)]  # Mon 12 → Sun 18


def day(date, kind="rest", minutes=0, intensity="recovery", tss=0, steps=None):
    return {"date": date, "kind": kind, "title": "x", "description": "", "duration_min": minutes,
            "intensity": intensity, "target_tss": tss, "bike": "none", "terrain": "", "weather_note": "",
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
    args = dict(dates=DATES, cfg=CFG, ctl=50, done_tss=0, recovery=False, forecast=[])
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
    assert any("two hard days" in e for e in run(p))


def test_steps_must_match_duration():
    p = good_plan()
    p["days"][0]["duration_min"] = 90
    assert any("steps sum to 60 min" in e for e in run(p))


def test_outdoor_needs_rideable_weather():
    p = good_plan()
    fc = [{"date": DATES[5], "temp_c": 1, "templow_c": -3, "precip_mm": 0, "wind_ms": 3, "condition": "sunny"}]
    assert any("not rideable" in e for e in run(p, forecast=fc))
    fc[0]["temp_c"] = 10
    assert run(p, forecast=fc) == []


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
