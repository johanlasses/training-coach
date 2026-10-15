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
    if i == 1:
        days.append({"date": d, "kind": "zwift", "title": "Sweet spot 3×12", "description": "Steady sweet spot, smooth pedalling.",
                     "duration_min": 66, "intensity": "sweetspot", "target_tss": 70, "bike": "trainer", "terrain": "",
                     "steps": [step("warmup", duration_s=600, power_low=0.45, power_high=0.75),
                               step("intervals", repeat=3, on_s=720, off_s=240, on_power=0.9, off_power=0.55, cadence=90, text="Settle in"),
                               step("cooldown", duration_s=480, power_low=0.6, power_high=0.4)]})
    elif i == 5:
        days.append({"date": d, "kind": "outdoor", "title": "Long gravel endurance", "description": "Z2, fuel every 30 min.",
                     "duration_min": 150, "intensity": "endurance", "target_tss": 120, "bike": "gravel",
                     "terrain": "Forest roads north of Västerås", "steps": []})
    else:
        days.append({"date": d, "kind": "rest", "title": "Rest", "description": "Off the bike.", "duration_min": 0,
                     "intensity": "recovery", "target_tss": 0, "bike": "none", "terrain": "", "steps": []})
plan_id = db.save_plan(ws.isoformat(), datetime.now().isoformat(timespec="seconds"), "demo",
                       {"week_focus": "Build aerobic base", "rationale": "Demo plan.", "days": days, "block_week": 2}, [])

# Coach chat with a pending proposal, coach notes, Apple Health values and Claude usage
now = datetime.now().isoformat(timespec="seconds")
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
for i in range(28):
    d = (today - timedelta(days=i)).isoformat()
    db.set_health(d, "rhr", 50 + random.randint(-2, 3), "bpm")
    db.set_health(d, "hrv", 58 + random.randint(-8, 8), "ms")
    db.set_health(d, "sleep", round(7 + random.uniform(-1, 0.8), 2), "h")


class _Usage:
    def __init__(self, i, o):
        self.input_tokens, self.output_tokens, self.cache_read_input_tokens = i, o, 0


class _Msg:
    model = "claude-opus-5-5"

    def __init__(self, i, o):
        self.usage = _Usage(i, o)


for purpose, i, o in [("plan", 14200, 9800), ("analysis", 9100, 4200), ("chat", 16300, 1100)]:
    db.log_llm(now, purpose, _Msg(i, o))
db.save_analysis(datetime.now().isoformat(timespec="seconds"), "manual", "demo",
                 "## Current form\nDemo report.\n\n## Limiters\n- 20-min power lags the 5-min power.\n")
print(f"Seeded {len(db.activities())} rides into {data / 'training.db'}")
