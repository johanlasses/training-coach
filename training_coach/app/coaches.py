"""The selectable coaching styles. Each coach's philosophy is a markdown file in coaches/; the style rules
the plan validator enforces (on top of the shared safety rules) live here."""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

HERE = Path(__file__).parent / "coaches"

# Style rules a coach may override. Safety rules (availability, weekly TSS cap, power bounds, recovery weeks)
# are not here. "Hard" is judged from workout content, see coach.is_hard_day.
DEFAULT_RULES = {
    "max_consecutive_hard": 1,          # hard days in a row, counting rides already done
    "max_hard_sessions": {"beginner": 2, "intermediate": 2, "advanced": 2, "elite": 2},  # per week, by level
    "density_weeks": None,              # after this many weeks with the coach, cap hard sessions at 2
}
# Running: impact load recovers slower than cycling, so fewer hard runs, and none for a beginner's first weeks is
# the coach's call (philosophy), not a cap. The run durability guard (coach.run_errors) caps volume on top.
RUN_RULES = DEFAULT_RULES | {"max_hard_sessions": {"beginner": 1, "intermediate": 2, "advanced": 2, "elite": 3}}


@dataclass(frozen=True)
class Coach:
    key: str
    name: str
    short: str      # tab label on the Plan page
    summary: str
    overrides: dict = field(default_factory=dict)
    cheers: tuple[str, ...] = ()        # rotated through interval reps in Zwift, in the coach's voice
    last_rep: str = "Last one"
    sport: str = "cycling"              # cycling | running (sports.SPORTS)

    def rules(self, level: str = "intermediate", weeks_with_coach: int = 0) -> dict:
        """Concrete limits for this athlete: {"max_consecutive_hard": int, "max_hard_sessions": int}."""
        r = (RUN_RULES if self.sport == "running" else DEFAULT_RULES) | self.overrides
        max_hard = r["max_hard_sessions"].get(level, 2)
        if r["density_weeks"] and weeks_with_coach >= r["density_weeks"]:
            max_hard = min(max_hard, 2)
        return {"max_consecutive_hard": r["max_consecutive_hard"], "max_hard_sessions": max_hard}

    @property
    def philosophy(self) -> str:
        return _load(self.key)


COACHES = {c.key: c for c in [
    Coach("pogacar", "Pogačar (San Millán)", "Pogačar",
          "San Millán's zone 2 method: big zone 2 volume, few but very hard sessions.",
          {"max_hard_sessions": {"beginner": 1, "intermediate": 2, "advanced": 2, "elite": 2}},
          cheers=("Smooth and controlled, quality over heroics",
                  "Breathe low and steady, relax the shoulders",
                  "Even pacing, it should feel hardest at the end, not now",
                  "Stay on top of the gear, don't fight the bike"),
          last_rep="Last one. Same power, same form"),
    Coach("carmichael", "Carmichael (Time-Crunched)", "Carmichael",
          "Little time, high intensity density: 3–4 interval sessions a week.",
          {"max_consecutive_hard": 2, "density_weeks": 8,
           "max_hard_sessions": {"beginner": 2, "intermediate": 3, "advanced": 4, "elite": 4}},
          cheers=("No filler, make this one count!",
                  "Steady at threshold. This is what wins on the climbs",
                  "Hold the number. You've got this!",
                  "Settle in fast, then lock onto the number"),
          last_rep="Last rep. Hit the number all the way to the line"),
    Coach("nextlevel", "Next Level", "Next Level",
          "Takes you from your current level to the next with proven periodisation.",
          cheers=("Same power as the first rep, steady cadence",
                  "Good work, keep the upper body quiet",
                  "Halfway or not, same power and cadence as rep one",
                  "Pace it, don't race it"),
          last_rep="Final rep, finish it well"),
    Coach("almgren", "Almgren (double threshold)", "Almgren",
          "Easy running, controlled threshold work, hills and strides; double threshold only above ~75 km a week.",
          {"max_hard_sessions": {"beginner": 1, "intermediate": 2, "advanced": 3, "elite": 3}}, sport="running"),
    Coach("runnextlevel", "Next Level Run", "Next Level Run",
          "80/20 running around work and family: one or two quality runs, a growing long run, deloads and strength.",
          sport="running"),
]}
DEFAULTS = {"cycling": "nextlevel", "running": "runnextlevel"}


def get(key: str | None, sport: str | None = None) -> Coach:
    """The coach by key; an unknown key (or one from the other sport) falls back to that sport's default."""
    c = COACHES.get(key or "")
    if c and (sport is None or c.sport == sport):
        return c
    return COACHES[DEFAULTS[sport or "cycling"]]


def for_sport(sport: str) -> list[Coach]:
    return [c for c in COACHES.values() if c.sport == sport]


def name_of(key: str | None) -> str:
    return COACHES[key].name if key in COACHES else "Original coach"


def short_name_of(key: str | None) -> str:
    return COACHES[key].short if key in COACHES else "Original"


@cache
def _load(key: str) -> str:
    return (HERE / f"{key}.md").read_text(encoding="utf-8")
