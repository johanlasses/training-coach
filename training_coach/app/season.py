"""Season phase and daylight for the Mälardalen region (no external deps)."""
from __future__ import annotations

import math
from datetime import date

LAT = 59.6  # Västerås / Mälardalen


def phase(d: date) -> str:
    m = d.month
    if m in (11, 12, 1, 2, 3):
        return "indoor"       # dark, cold, wet roads with salt: Zwift-first
    if m in (4, 10):
        return "shoulder"     # outdoor on good weekend days, Zwift for quality work
    return "outdoor"          # May–Sep: road and gravel season


PHASE_TEXT = {
    "indoor": "Indoor season (Nov–Mar): Zwift is the default; outdoor only on unusually good weekend days.",
    "shoulder": "Shoulder season (Apr/Oct): outdoor endurance on good weekend days, structured work on Zwift.",
    "outdoor": "Outdoor season (May–Sep): prefer outdoor road/gravel rides; Zwift for short or bad-weather days.",
}


def daylight(d: date, lat: float = LAT) -> tuple[float, str, str]:
    """(hours of daylight, approx sunrise, approx sunset) in local clock time.

    NOAA-style approximation; good to ~10 min, enough for "can I ride after work".
    """
    n = d.timetuple().tm_yday
    decl = math.radians(23.44) * math.sin(math.radians(360 / 365 * (n - 81)))
    cos_h = (math.cos(math.radians(90.833)) - math.sin(math.radians(lat)) * math.sin(decl)) / (
        math.cos(math.radians(lat)) * math.cos(decl))
    cos_h = max(-1.0, min(1.0, cos_h))
    hours = 2 * math.degrees(math.acos(cos_h)) / 15
    # Solar noon in Västerås ≈ 12:24 CET / 13:24 CEST (16.5°E; equation of time ignored)
    noon = 12.4 + (1 if _is_dst(d) else 0)
    return round(hours, 1), _hm(noon - hours / 2), _hm(noon + hours / 2)


def _is_dst(d: date) -> bool:
    # EU DST: last Sunday of March → last Sunday of October
    def last_sunday(month: int) -> date:
        last = date(d.year, month, 31)
        return date(d.year, month, 31 - (last.weekday() + 1) % 7)
    return last_sunday(3) <= d < last_sunday(10)


def _hm(h: float) -> str:
    h = h % 24
    return f"{int(h):02d}:{int(round((h % 1) * 60)) % 60:02d}"
