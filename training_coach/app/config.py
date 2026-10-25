"""User settings, edited on the in-app Settings page and stored in the add-on's DB (/data).

Defaults live here; the DB holds only what the user changed. For local development,
TC_DATA_DIR points at a writable dir and ANTHROPIC_API_KEY can stand in for the stored key.
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .db import DB

DATA_DIR = Path(os.environ.get("TC_DATA_DIR", "/data"))

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
SECRETS = {"anthropic_api_key", "strava_client_secret"}
SYNC_INTERVALS = {0: "Off", 15: "Every 15 min", 30: "Every 30 min", 60: "Every hour", 120: "Every 2 hours",
                  240: "Every 4 hours"}

# Athlete levels by FTP W/kg, with Zwift's classic categories as a reference. Zwift A starts at 4.0 W/kg,
# but that's a strong amateur (Coggan's power profile puts domestic elite around 5 W/kg), not elite.
LEVELS = {
    "beginner": ("Beginner", "under 2.5 W/kg, Zwift D"),
    "intermediate": ("Intermediate", "2.5–3.2 W/kg, Zwift C"),
    "advanced": ("Advanced", "3.2–5.0 W/kg, Zwift B/A"),
    "elite": ("Elite", "5.0+ W/kg, racing at elite level"),
}


def level_for_wkg(wkg: float) -> str:
    return "beginner" if wkg < 2.5 else "intermediate" if wkg < 3.2 else "advanced" if wkg < 5.0 else "elite"


@dataclass
class Config:
    anthropic_api_key: str = ""
    strava_client_id: str = ""
    strava_client_secret: str = ""
    weight_kg: float = 75.0
    ftp_override: int = 0
    ftp_tests: list[dict] = field(default_factory=list)  # [{date, ftp, protocol}] set from field tests on Plan
    lthr: int = 165
    available_days: list[str] = field(default_factory=lambda: ["mon", "tue", "wed", "thu", "sat", "sun"])
    max_weekday_minutes: int = 75
    max_weekend_minutes: int = 180
    notify_service: str = ""  # Settings suggests the athlete's phone
    goals: str = ""
    athlete_level: str = "intermediate"
    sessions_per_week: int = 5
    default_coach: str = "nextlevel"
    # Schedules (Settings → Schedules & API usage). Claude jobs are off until switched on.
    sync_interval_min: int = 30           # 0 = off
    zwift_refresh: bool = True
    notify_time: str = "07:00"            # "" = off
    auto_plan: bool = False               # plans next week
    auto_plan_day: str = "sun"
    auto_plan_time: str = "20:00"
    auto_analysis: bool = False
    auto_analysis_day: str = "mon"
    auto_analysis_time: str = "07:00"
    # Apple Health sensors from the HA iOS companion app (Profile). Empty = not used.
    health_rhr_entity: str = ""
    health_hrv_entity: str = ""
    health_sleep_entity: str = ""
    health_weight_entity: str = ""
    health_vo2max_entity: str = ""

    def max_minutes(self, weekday: int) -> int:
        """Max training minutes for a weekday (0=Mon); 0 when the day is unavailable."""
        if WEEKDAYS[weekday] not in self.available_days:
            return 0
        return self.max_weekend_minutes if weekday >= 5 else self.max_weekday_minutes


FIELD_TYPES = {f.name: f.type for f in fields(Config)}


def load_config(db: DB) -> Config:
    stored = db.get("settings") or {}
    cfg = Config(**{k: v for k, v in stored.items() if k in FIELD_TYPES})
    if not cfg.anthropic_api_key:
        cfg.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    return cfg


def save_config(db: DB, cfg: Config) -> None:
    db.set("settings", asdict(cfg))
