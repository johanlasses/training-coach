"""In-depth AI analysis of the athlete's current state."""
from __future__ import annotations

import json
from datetime import datetime

from . import llm
from .config import Config
from .db import DB

SYSTEM = """You are a sharp, honest cycling coach and sports scientist analysing one amateur road and \
gravel cyclist's training (Zwift indoors, road/gravel outside, Mälardalen, Sweden). Goal: general \
fitness and FTP gain. You get a JSON snapshot: load (CTL/ATL/TSB), 12 weeks of volume, power curves \
(90 d and 365 d best efforts), eFTP trend, zone distribution, Pw:HR decoupling, recent rides, season \
and daylight.

Go deeper than Zwift or Strava summaries. Reason from the numbers: compare 90-day and 365-day power \
curves to find strengths and limiters (e.g. strong 5 min vs. weak 20 min suggests aerobic/threshold \
work), judge intensity distribution, consistency, ramp rate, fatigue and monotony, aerobic durability \
from decoupling. Be direct — if training is unstructured or too much Z3, say so. Point out when data \
is thin (e.g. eFTP based on no real maximal efforts, HR-only rides) instead of over-interpreting.

Write in English, Markdown, with exactly these sections:
## Current form
## What's working
## Limiters
## Risks
## Focus for the next 4 weeks
## Suggested test
Keep it under ~600 words. Use concrete numbers from the data."""


async def run_analysis(db: DB, cfg: Config, snapshot: dict, kind: str) -> int:
    msg = await llm.run(cfg.anthropic_api_key, SYSTEM, [{"role": "user", "content":
                        f"```json\n{json.dumps(snapshot, indent=1)}\n```"}], effort="high")
    return db.save_analysis(datetime.now().isoformat(timespec="seconds"), kind, llm.MODEL, llm.text_of(msg))
