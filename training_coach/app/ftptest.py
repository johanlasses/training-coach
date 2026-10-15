"""FTP field tests: which effort each protocol measures, and the FTP it implies."""
from __future__ import annotations

from . import workouts

# protocol: (label, effort seconds, FTP fraction of that effort's best power)
PROTOCOLS = {
    "ramp": ("Ramp test", 60, 0.75),
    "cts_8min": ("CTS field test", 480, 0.90),
    "twenty_min": ("20-min test", 1200, 0.95),
}


# What an all-out effort averages, as a fraction of FTP, for the TSS estimate (freeride steps count as 0.6 otherwise)
EFFORT_POWER = {"twenty_min": 1.05, "cts_8min": 1.11}
RAMP_FAIL = 1.35  # where most riders stop a ramp test


def estimated_tss(protocol: str, steps: list[dict]) -> float:
    """Planned TSS of a test: all-out freeride efforts at their typical power; a ramp cut where riders fail."""
    steps = [dict(s) for s in steps]
    if protocol == "ramp":
        cut = next((i for i, s in enumerate(steps) if s["type"] == "steady" and s["power_low"] > RAMP_FAIL), None)
        if cut is not None:
            rest = [s for s in steps[cut:] if s["type"] == "cooldown"]
            steps = steps[:cut] + rest
    elif protocol in EFFORT_POWER:
        for s in steps:
            if s["type"] == "freeride" and s["duration_s"] >= 300:
                s.update(type="steady", power_low=EFFORT_POWER[protocol], power_high=EFFORT_POWER[protocol])
    return workouts.estimated_tss(steps)


def estimate(protocol: str, mmp: dict[int, float] | None) -> tuple[int, str] | None:
    """(FTP watts, how it was worked out) from a test ride's best efforts; None if the ride lacks that effort."""
    if protocol not in PROTOCOLS or not mmp:
        return None
    label, secs, frac = PROTOCOLS[protocol]
    best = mmp.get(secs)
    if not best:
        return None
    effort = f"{secs // 60} min" if secs >= 120 else "1 min"
    return round(best * frac), f"{label}: {frac:.0%} of best {effort} {round(best)} W"
