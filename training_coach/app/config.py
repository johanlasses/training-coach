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
    weather_entity: str = "weather.forecast_hem"
    notify_service: str = "notify.johan_iphone"
    rideable_min_temp_c: float = 3.0
    rideable_max_wind_ms: float = 10.0
    rideable_max_precip_mm: float = 2.0

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
