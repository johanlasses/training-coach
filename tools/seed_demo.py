#!/usr/bin/env python3
"""Fill a local DB with ~4 months of synthetic rides and a sample plan, for UI work without Strava.

TC_DATA_DIR=/tmp/tc-demo .venv/bin/python tools/seed_demo.py
"""
import os
import random
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "training_coach"))

from app import stats  # noqa: E402
from app.config import Config  # noqa: E402
from app.db import DB  # noqa: E402
from app.sync import process_streams, recompute  # noqa: E402

random.seed(7)
data = Path(os.environ.get("TC_DATA_DIR", "data"))
data.mkdir(exist_ok=True)
db = DB(data / "training.db")
today = date.today()

KINDS = [  # name, sport, minutes, base watts, hard blocks (start_frac, len_s, watts)
    ("Zwift - Endurance in Watopia", "VirtualRide", 60, 165, []),
    ("Zwift - Sweet spot 3x12", "VirtualRide", 70, 160, [(0.2, 720, 245), (0.45, 720, 245), (0.7, 720, 245)]),
    ("Zwift - VO2 5x4", "VirtualRide", 60, 150, [(0.25 + i * 0.1, 240, 310) for i in range(5)]),
    ("Morning gravel loop", "GravelRide", 150, 170, [(0.5, 300, 300)]),
    ("Road ride Mälaren", "Ride", 120, 180, [(0.4, 1200, 255)]),
]

aid = 1000
for back in range(120, -1, -1):
    d = today - timedelta(days=back)
    if random.random() < 0.45 or (d.weekday() in (0, 4) and random.random() < 0.8):
        continue
    name, sport, minutes, base, blocks = random.choice(KINDS[:3] if d.month in (11, 12, 1, 2, 3) or d.weekday() < 5 else KINDS)
    n = minutes * 60
    watts = [max(0, random.gauss(base, 18)) for _ in range(n)]
    for frac, length, w in blocks:
        s = int(frac * n)
        for i in range(s, min(n, s + length)):
            watts[i] = max(0, random.gauss(w + back * -0.15, 8))
    hr = [110 + w * 0.17 + i / n * 6 for i, w in enumerate(watts)]
    start = datetime.combine(d, datetime.min.time()).replace(hour=17 if d.weekday() < 5 else 9)
    aid += 1
    db.upsert_activity_summary({
        "id": aid, "name": name, "sport_type": sport, "start_date": start.isoformat() + "Z",
        "start_date_local": start.isoformat(), "distance": minutes / 60 * (31000 if sport != "GravelRide" else 25000),
        "moving_time": n, "elapsed_time": n + 120, "elevation_gain": random.randint(100, 900),
        "trainer": int(sport == "VirtualRide"), "avg_watts": sum(watts) / n, "weighted_avg_watts": None,
        "max_watts": max(watts), "device_watts": 1, "avg_hr": sum(hr) / n, "max_hr": max(hr),
        "kilojoules": sum(watts) / 1000, "avg_speed": 8.5,
    })
    db.update_activity(aid, streams_fetched=1, **process_streams(
        {"time": list(range(n)), "watts": watts, "heartrate": hr}))

cfg = Config()
recompute(db, cfg)


def step(type_, **kw):
    s = {"type": type_, "duration_s": 0, "power_low": 0, "power_high": 0, "repeat": 0, "on_s": 0,
         "off_s": 0, "on_power": 0, "off_power": 0, "cadence": 0, "text": ""}
    s.update(kw)
    return s


ws = stats.week_start(today)
days = []
for i in range(7):
    d = (ws + timedelta(days=i)).isoformat()
    if i == today.weekday():  # the latest ride follows this one, so the Overview shows ridden vs planned
        days.append({"date": d, "kind": "zwift", "title": "Sweet spot 3×12", "description": "Steady sweet spot, smooth pedalling.",
                     "duration_min": 66, "intensity": "sweetspot", "target_tss": 70, "bike": "trainer", "terrain": "",
                     "steps": [step("warmup", duration_s=600, power_low=0.45, power_high=0.75),
                               step("intervals", repeat=3, on_s=720, off_s=240, on_power=0.9, off_power=0.55, cadence=90, text="Settle in"),
                               step("cooldown", duration_s=480, power_low=0.6, power_high=0.4)]})
    elif i == 5 and i != today.weekday():
        days.append({"date": d, "kind": "outdoor", "title": "Long gravel endurance", "description": "Z2, fuel every 30 min.",
                     "duration_min": 150, "intensity": "endurance", "target_tss": 120, "bike": "gravel",
                     "terrain": "Forest roads north of Västerås", "steps": []})
    else:
        days.append({"date": d, "kind": "rest", "title": "Rest", "description": "Off the bike.", "duration_min": 0,
                     "intensity": "recovery", "target_tss": 0, "bike": "none", "terrain": "", "steps": []})
# Earlier weeks: two with Carmichael, then the athlete switched to Next Level
for n, key in ((3, "carmichael"), (2, "carmichael"), (1, "nextlevel")):
    w = ws - timedelta(weeks=n)
    db.save_plan(w.isoformat(), f"{w - timedelta(days=1)}T20:00:00", "demo",
                 {"week_focus": "Demo week", "rationale": "", "block_week": 4 - n,
                  "days": [dict(d, date=(w + timedelta(days=i)).isoformat()) for i, d in enumerate(days)]}, [], key)
switched = f"{ws - timedelta(weeks=1, days=2)}T18:30:00"
db.log(switched, "coach", "Coach: Carmichael (Time-Crunched) → Next Level", {"from": "carmichael", "to": "nextlevel"})
db.log(f"{ws - timedelta(weeks=5)}T21:00:00", "profile", "Goals: none → “Ride a 160 km gravel event in June”",
       {"field": "goals", "old": "", "new": "Ride a 160 km gravel event in June"})
db.log(f"{ws - timedelta(weeks=2)}T07:10:00", "profile", "Weight: 78 kg → 76.5 kg", {"field": "weight_kg", "old": 78.0, "new": 76.5})
plan_id = db.save_plan(ws.isoformat(), datetime.now().isoformat(timespec="seconds"), "demo",
                       {"week_focus": "Build aerobic base", "rationale": "Demo plan.", "days": days, "block_week": 2}, [],
                       "nextlevel")

# Today's ride: the planned workout, with the last rep fading, and the coach's feedback on it
ftp = db.activities(limit=1)[0]["ftp_used"]
from app import workouts  # noqa: E402
plan_steps = next(d for d in days if d["date"] == today.isoformat())["steps"]
watts = []
for start, dur, lo, hi in workouts.segments(plan_steps):
    for t in range(dur):
        target = (lo + (hi - lo) * t / dur) * ftp
        fade = 0.93 if start >= 2 * 960 + 600 and lo >= 0.88 else 1
        watts.append(max(0, random.gauss(target * fade, 6)))
n = len(watts)
start = datetime.combine(today, datetime.min.time()).replace(hour=7)
aid += 1
db.upsert_activity_summary({
    "id": aid, "name": "Zwift - Sweet spot 3x12 in Watopia", "sport_type": "VirtualRide", "start_date": start.isoformat() + "Z",
    "start_date_local": start.isoformat(), "distance": 34000, "moving_time": n, "elapsed_time": n,
    "elevation_gain": 320, "trainer": 1, "avg_watts": sum(watts) / n, "weighted_avg_watts": None,
    "max_watts": max(watts), "device_watts": 1, "avg_hr": 146, "max_hr": 168, "kilojoules": sum(watts) / 1000, "avg_speed": 8.5})
db.update_activity(aid, streams_fetched=1, notified=1, feedback={
    "headline": "Two strong reps, the third faded",
    "notification": "Reps 1 and 2 sat right on 90% FTP. Rep 3 dropped about 7%, so the last block was tempo, not sweet "
                    "spot. Eat well tonight: tomorrow is rest, then the long gravel day.",
    "detail": "Reps 1 and 2 were textbook: within 2% of target, cadence steady.\n\nRep 3 averaged about 7% under "
              "target. That's the edge of your current sweet spot durability, and it's the thing to build. Next time, "
              "start rep 3 at the low end of the range rather than fading into it.\n\nHRV and resting HR are on "
              "baseline, so this wasn't fatigue showing. **Next:** rest tomorrow, then Saturday's long gravel ride in Z2.",
    "coach": "nextlevel", "model": "claude-haiku-5-5", "at": datetime.now().isoformat(timespec="seconds")},
    **process_streams({"time": list(range(n)), "watts": watts, "heartrate": [130 + w * 0.08 for w in watts]}))
recompute(db, cfg)

# Coach chat: an earlier talk with Carmichael, the switch, a pending proposal; notes, memory, Apple Health, usage
now = datetime.now().isoformat(timespec="seconds")
for ago, at, role, key, text in [
    (12, "19:02", "user", "carmichael", "Can I swap Thursday's intervals for a club ride? It's usually fast."),
    (12, "19:03", "assistant", "carmichael", "Yes, if you treat it as the session: sit in for the first hour, then go "
     "hard on the climbs. Skip Friday's openers."),
    (12, "19:10", "user", "carmichael", "Deal. Three interval days a week is a lot with work though."),
    (9, "08:15", "user", "nextlevel", "I switched to you because the interval weeks wore me down. Where do we start?"),
    (9, "08:16", "assistant", "nextlevel", "Good call to say it. You come off two dense weeks, so we keep your fitness and "
     "drop to two quality days: one sweet spot, one long ride. Same weekly hours, less intensity."),
    (1, "20:40", "user", "nextlevel", "Legs felt flat on today's endurance ride."),
    (1, "20:41", "assistant", "nextlevel", "Your form is −14, which fits a build week. Sleep was short two nights in a "
     "row too. Keep tomorrow's sweet spot, but start at the low end of the range."),
    (1, "20:43", "user", "nextlevel", "OK. And I'm on nights at work in March, worth knowing."),
    (1, "20:44", "assistant", "nextlevel", "Noted. I'll plan shorter weekday sessions then."),
]:
    db.add_chat(f"{today - timedelta(days=ago)}T{at}:00", role, key, text,
                remembered=["Works night shifts every March, so short weekday sessions then"] if text.startswith("Noted") else ())
for created, key, text in [(f"{today - timedelta(days=12)}T19:11:00", "carmichael", "Rides a fast club ride on Thursdays"),
                           (f"{today - timedelta(days=9)}T08:16:00", "nextlevel", "Three interval days a week wore them down"),
                           (f"{today - timedelta(days=1)}T20:44:00", "nextlevel", "Works night shifts every March, so short weekday sessions then")]:
    db.add_memory(created, key, text)
db.add_note(now, "injury", (today - timedelta(days=40)).isoformat(), (today - timedelta(days=33)).isoformat(),
            "Sore left knee after a long climbing day")
later = [d for d in days if d["date"] >= today.isoformat()]
db.add_chat(now, "user", "nextlevel", "I'm away on a work trip for two days later this week. Also, there's an 80 km gravel "
            "race in three weeks I'd like to ride well, though my main goal stays the same.")
db.add_chat(now, "assistant", "nextlevel", "Good to know. I've moved the quality session so it doesn't fall on your travel "
            "days, and saved the race so the next weeks build towards it with a light taper.\n\nKeep the rides on "
            "either side of the trip easy: travel is fatigue too.",
            {"plan_id": plan_id, "days": [dict(d, kind="rest", title="Travel day: rest", duration_min=0, steps=[])
                                          for d in later[:2]]} if later else None, [],
            [f"Gravel race 80 km ({(today + timedelta(days=21)).strftime('%-d %b')})"])
db.add_note(now, "event", (today + timedelta(days=21)).isoformat(), (today + timedelta(days=21)).isoformat(),
            "B-race: 80 km gravel, wants to ride it well")
db.add_note(now, "absence", (today + timedelta(days=2)).isoformat(), (today + timedelta(days=3)).isoformat(), "Work trip")
for i in range(35):  # a full 7-day week and 28-day baseline, so the Overview's readiness gauges show
    d = (today - timedelta(days=i)).isoformat()
    db.set_health(d, "rhr", 50 + random.randint(-2, 3), "bpm")
    db.set_health(d, "hrv", 58 + random.randint(-8, 8), "ms")
    db.set_health(d, "sleep", round(7 + random.uniform(-1, 0.8), 2), "h")

db.set("settings", {"health_rhr_entity": "sensor.demo_iphone_resting_heart_rate",
                    "health_hrv_entity": "sensor.demo_iphone_heart_rate_variability",
                    "health_sleep_entity": "sensor.demo_iphone_sleep_duration"})
db.add_chat(datetime.now().isoformat(timespec="seconds"), "assistant", "nextlevel", "Yesterday's endurance ride is in the bank, 62 TSS right on "
            "plan, and three sessions in a row this week. Today is the sweet spot: start at the low end of the range, your sleep "
            "was short again.", kind="checkin", ask="I missed Threshold 2×15. Can you adjust the rest of my week so it "
            "still works, without cramming the missed session in?")
db.set("checkin_tried", today.isoformat())
db.set("checkin_day", today.isoformat())


class _Usage:
    def __init__(self, i, o):
        self.input_tokens, self.output_tokens, self.cache_read_input_tokens = i, o, 0


class _Msg:
    model = "claude-opus-5-5"

    def __init__(self, i, o):
        self.usage = _Usage(i, o)


for purpose, model, i, o in [("plan", "claude-opus-5-5", 14200, 9800), ("analysis", "claude-opus-5-5", 9100, 4200),
                             ("chat", "claude-sonnet-5-5", 16300, 1100), ("ride", "claude-haiku-5-5", 5200, 900)]:
    m = _Msg(i, o)
    m.model = model
    db.log_llm(now, purpose, m)
db.save_analysis(datetime.now().isoformat(timespec="seconds"), "manual", "demo",
                 "## Current form\nDemo report.\n\n## Limiters\n- 20-min power lags the 5-min power.\n")
print(f"Seeded {len(db.activities())} rides into {data / 'training.db'}")
