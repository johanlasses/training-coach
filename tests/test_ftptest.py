import pytest
from fastapi.testclient import TestClient

from app import coach, ftptest, llm, main, workouts
from app.config import load_config

from .test_coach import DATES, day, good_plan, run, sweetspot
from .test_workouts import step


def test_estimates_per_protocol():
    mmp = {60: 400, 480: 320, 1200: 290}
    assert ftptest.estimate("ramp", mmp)[0] == 300
    assert ftptest.estimate("cts_8min", mmp)[0] == 288
    assert ftptest.estimate("twenty_min", mmp)[0] == 276
    assert "90% of best 8 min 320 W" in ftptest.estimate("cts_8min", mmp)[1]
    assert ftptest.estimate("cts_8min", {60: 400}) is None
    assert ftptest.estimate("none", mmp) is None and ftptest.estimate("ramp", None) is None


def cts_steps():
    return [step("warmup", duration_s=1200, power_low=0.5, power_high=0.75), step("freeride", duration_s=480),
            step("steady", duration_s=600, power_low=0.5, power_high=0.5), step("freeride", duration_s=480),
            step("cooldown", duration_s=840, power_low=0.6, power_high=0.4)]


def test_test_day_counts_as_hard_and_keeps_its_tss():
    p = good_plan()
    # Test on Tue, between Mon sweetspot (not hard) and Wed threshold → two hard days in a row
    p["days"][1] = day(DATES[1], "zwift", 60, "endurance", 85, cts_steps()) | {"test_protocol": "cts_8min"}
    assert coach.is_hard_day(p["days"][1])
    errs = run(p)
    assert any("hard days in a row" in e for e in errs)
    assert 60 <= p["days"][1]["target_tss"] <= 75  # efforts at ~1.11 FTP, not the freeride default
    assert workouts.estimated_tss(cts_steps()) < 55


def ramp_steps(top=1.75):
    steps = [step("warmup", duration_s=300, power_low=0.45, power_high=0.55)]
    steps += [step("steady", duration_s=60, power_low=round(0.55 + 0.06 * i, 2), power_high=round(0.55 + 0.06 * i, 2))
              for i in range(int(round((top - 0.55) / 0.06)) + 1)]
    return steps + [step("cooldown", duration_s=600, power_low=0.5, power_high=0.4)]


def test_ramp_tss_stops_where_riders_fail():
    assert 30 <= ftptest.estimated_tss("ramp", ramp_steps()) <= 45
    assert ftptest.estimated_tss("ramp", ramp_steps()) < workouts.estimated_tss(ramp_steps())


def test_a_test_is_the_one_hard_touch_of_a_recovery_week():
    p = good_plan()
    for i in (0, 1, 2):
        p["days"][i] = day(DATES[i], "zwift", 45, "recovery", 0, [step("steady", duration_s=2700, power_low=0.5, power_high=0.5)])
    p["days"][3] = day(DATES[3], "zwift", 36, "vo2max", 0, ramp_steps()) | {"test_protocol": "ramp"}
    errs = run(p, recovery=True)
    assert not any("no vo2max" in e or "hard days" in e for e in errs), errs


def test_day_before_a_test_must_be_easy_and_done_tests_count_as_hard():
    p = good_plan()
    p["days"][3] = day(DATES[3], "zwift", 60, "endurance", 0, cts_steps()) | {"test_protocol": "cts_8min"}
    assert any("day before an FTP test" in e for e in run(p))  # Wed is threshold
    # A done ride with a maximal effort is hard even with a diluted whole-ride IF
    p = good_plan()
    p["days"] = p["days"][1:]
    done = [{"date": DATES[0], "if": 0.82, "maximal_effort": True}]
    p["days"][0] = day(DATES[1], "zwift", 60, "threshold", 0, sweetspot(60))
    assert any("hard days in a row" in e for e in run(p, dates=DATES[1:], done_rides=done))


def test_notes_make_absence_rest_and_let_events_run_long():
    p = good_plan()
    p["days"][6] = day(DATES[6], "outdoor", 240, "mixed", 220)  # an event longer than the weekend limit
    notes = [{"kind": "absence", "start": DATES[0], "end": DATES[1], "text": "trip"},
             {"kind": "event", "start": DATES[6], "end": DATES[6], "text": "gravel race"}]
    errs = run(p, notes=notes)
    assert sum("athlete is away" in e for e in errs) == 2
    assert not any("exceeds availability" in e for e in errs)


def test_maximal_effort_flag():
    from app.stats import maximal_effort
    assert maximal_effort({"ftp_used": 250, "mmp": {1200: 240}})
    assert maximal_effort({"ftp_used": 250, "mmp": {300: 290}})
    assert maximal_effort({"ftp_used": 250, "mmp": {300: 280, 480: 270}})  # a CTS effort at ~1.08
    assert not maximal_effort({"ftp_used": 250, "mmp": {300: 270, 1200: 220}})
    assert not maximal_effort({"ftp_used": None, "mmp": {1200: 400}})


def test_schema_requires_test_protocol():
    item = coach.PLAN_SCHEMA["properties"]["days"]["items"]
    assert "test_protocol" in item["required"]
    assert set(item["properties"]["test_protocol"]["enum"]) == {"none", "ramp", "cts_8min", "twenty_min"}


def test_ramp_test_exports_to_zwo():
    steps = [step("warmup", duration_s=300, power_low=0.45, power_high=0.55)]
    steps += [step("steady", duration_s=60, power_low=round(0.55 + 0.06 * i, 2), power_high=round(0.55 + 0.06 * i, 2))
              for i in range(21)]
    xml = workouts.to_zwo("Ramp", "", steps)
    assert xml.count("<SteadyState") == 21 and 'Power="1.75"' in xml


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)

    async def fake_check(key):
        return None

    monkeypatch.setattr(llm, "check_key", fake_check)
    with TestClient(main.app) as c:
        yield c


def test_plan_page_offers_the_test_result(client):
    st = main.app.state.st
    today = main.date.today()
    ws = main.stats.week_start(today)
    d = day(today.isoformat(), "zwift", 60, "endurance", 80, cts_steps()) | {"test_protocol": "cts_8min"}
    st.db.save_plan(ws.isoformat(), "t", "m", {"week_focus": "Test", "rationale": "", "block_week": 1, "days": [d]}, [], "carmichael")
    st.db.upsert_activity_summary({"id": 1, "name": "CTS", "sport_type": "VirtualRide",
                                   "start_date": f"{today}T16:00:00Z", "start_date_local": f"{today}T18:00:00",
                                   "moving_time": 3600, "trainer": 1})
    st.db.update_activity(1, mmp={60: 420, 480: 330, 1200: 300}, streams_fetched=1)
    html = client.get("/plan?coach=carmichael").text
    assert "Estimated FTP 297 W" in html and "/ftp/apply" in html
    assert st.recent_tests(today + main.timedelta(days=1))[0]["protocol"] == "cts_8min"

    r = client.post("/ftp/apply", data={"ftp": "297", "date": today.isoformat(), "week": ws.isoformat(),
                                        "protocol": "cts_8min"},
                    headers={"X-Ingress-Path": "/api/hassio_ingress/abc"}, follow_redirects=False)
    assert r.headers["location"].startswith("/api/hassio_ingress/abc/plan")
    cfg = load_config(st.db)
    assert cfg.ftp_override == 0 and cfg.ftp_tests == [{"date": today.isoformat(), "ftp": 297, "protocol": "cts_8min"}]
    assert "Your FTP from this day on" in client.get("/plan?coach=carmichael").text
    assert st.recent_tests(today + main.timedelta(days=1))[0] | {} == {
        "date": today.isoformat(), "protocol": "cts_8min", "done": True, "ftp_set_w": 297}
    assert "297 W" in client.get("/profile").text
    client.post("/ftp/apply", data={"ftp": "9000", "date": today.isoformat(), "week": ws.isoformat()})
    assert load_config(st.db).ftp_tests[0]["ftp"] == 297
    client.post(f"/ftp/tests/delete?date={today.isoformat()}")
    assert load_config(st.db).ftp_tests == []


def test_a_test_sets_ftp_from_its_date_only():
    from datetime import date

    from app.sync import ftp_at
    tests = [{"date": "2026-10-15", "ftp": 300, "protocol": "ramp"}]
    acts = [{"start_date_local": "2026-09-01T10:00:00", "mmp": {1200: 263}}]  # eFTP 250
    assert ftp_at(acts, date(2026, 10, 14), 0, tests) == (250, "eftp")   # before the test: unchanged
    assert ftp_at(acts, date(2026, 10, 15), 0, tests) == (300, "test")
    assert ftp_at(acts, date(2027, 1, 7), 0, tests) == (300, "test")     # 12 weeks later
    assert ftp_at(acts, date(2027, 1, 8), 0, tests)[1] != "test"          # expired
    assert ftp_at(acts, date(2026, 10, 20), 280, tests) == (280, "override")
