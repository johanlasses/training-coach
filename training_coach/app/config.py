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
    lthr: int = 165
    available_days: list[str] = field(default_factory=lambda: ["mon", "tue", "wed", "thu", "sat", "sun"])
    max_weekday_minutes: int = 75
    max_weekend_minutes: int = 180
    notify_service: str = "notify.johan_iphone"
    goals: str = ""
    athlete_level: str = "intermediate"
    sessions_per_week: int = 5
    default_coach: str = "nextlevel"

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
