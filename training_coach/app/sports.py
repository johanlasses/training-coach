"""Which activities the app keeps, and which sport each is. Activities carry Strava's sport types (Garmin's are mapped
onto them); everything else asks `kind()` instead of comparing sport types itself.

ride: every view and number (power curve, zones, eFTP, Send to Zwift). run: run load (hrTSS/rTSS), the running coach,
the run durability guard. walk: load only (hrTSS), and a long one can stand in for an easy session (coach.compliance).
"""
from __future__ import annotations

RIDE_TYPES = {"Ride", "VirtualRide", "GravelRide", "MountainBikeRide"}
RUN_TYPES = {"Run", "TrailRun", "VirtualRun"}
WALK_TYPES = {"Walk", "Hike"}
KEPT = RIDE_TYPES | RUN_TYPES | WALK_TYPES

# The planner's sports (Profile → Sports) and the activity kind each one trains
SPORTS = {"cycling": "Cycling", "running": "Running"}
ACTIVITY_KIND = {"cycling": "ride", "running": "run"}


def kind(sport_type: str | None) -> str | None:
    """ride | run | walk, or None for an activity the app doesn't keep."""
    return ("ride" if sport_type in RIDE_TYPES else "run" if sport_type in RUN_TYPES
            else "walk" if sport_type in WALK_TYPES else None)


def is_ride(a: dict) -> bool:
    return a.get("sport_type") in RIDE_TYPES


def rides(acts: list[dict]) -> list[dict]:
    return [a for a in acts if a.get("sport_type") in RIDE_TYPES]


def runs(acts: list[dict]) -> list[dict]:
    return [a for a in acts if a.get("sport_type") in RUN_TYPES]


def of_sport(acts: list[dict], sport: str) -> list[dict]:
    """The activities a planned session of `sport` can match."""
    return runs(acts) if sport == "running" else rides(acts)


def day_sport(d: dict) -> str:
    """A planned day's sport. Plans from before 0.11.0 have no sport: they're cycling."""
    return d.get("sport") or "cycling"


def day_key(d: dict) -> str:
    """One session per sport per day: "2026-11-02/running". Keys statuses, uploads and chat changes."""
    return f"{d['date']}/{day_sport(d)}"
