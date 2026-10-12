"""Structured workouts (%FTP steps) → Zwift .zwo XML and chart segments."""
from __future__ import annotations

import re
from xml.sax.saxutils import quoteattr

# A step as produced by the coach (all keys always present; irrelevant ones are 0/"").
#   type: warmup | cooldown | steady | intervals | freeride
#   duration_s, power_low, power_high       (warmup/cooldown/steady/freeride)
#   repeat, on_s, off_s, on_power, off_power (intervals)
#   cadence (0 = none), text (shown in Zwift at step start)
#   cues: [{offset_s, text}] more messages during the step, offset from the step start (for intervals,
#         from the start of the whole block). Optional; older plans don't have it.


def step_seconds(step: dict) -> int:
    if step["type"] == "intervals":
        return step["repeat"] * (step["on_s"] + step["off_s"])
    return step["duration_s"]


def total_seconds(steps: list[dict]) -> int:
    return sum(step_seconds(s) for s in steps)


def segments(steps: list[dict]) -> list[tuple[int, int, float, float]]:
    """Flatten to (start_s, duration_s, power_start, power_end) segments for charts / TSS."""
    out = []
    t = 0
    for s in steps:
        if s["type"] == "intervals":
            for _ in range(s["repeat"]):
                out.append((t, s["on_s"], s["on_power"], s["on_power"]))
                t += s["on_s"]
                if s["off_s"]:
                    out.append((t, s["off_s"], s["off_power"], s["off_power"]))
                    t += s["off_s"]
        elif s["type"] == "freeride":
            out.append((t, s["duration_s"], 0.6, 0.6))
            t += s["duration_s"]
        else:
            out.append((t, s["duration_s"], s["power_low"], s["power_high"]))
            t += s["duration_s"]
    return out


def is_hard(steps: list[dict]) -> bool:
    """A quality session by content, whatever it's called: ≥10 min at ≥88% FTP or ≥5 min above 105%.
    Openers (a few short efforts above 105%) stay under the second bar."""
    near_threshold = above = 0
    for _, d, a, b in segments(steps):
        p = (a + b) / 2
        near_threshold += d if p >= 0.88 else 0
        above += d if p > 1.05 else 0
    return near_threshold >= 600 or above >= 300


def estimated_tss(steps: list[dict]) -> float:
    """TSS from the planned profile (NP ≈ 4th-power mean of planned %FTP)."""
    segs = segments(steps)
    secs = sum(d for _, d, _, _ in segs)
    if not secs:
        return 0.0
    m4 = sum(d * ((a + b) / 2) ** 4 for _, d, a, b in segs) / secs
    intensity = m4 ** 0.25
    return round(secs / 3600 * intensity**2 * 100, 1)


def svg_profile(steps: list[dict], width: int = 600, height: int = 80) -> str:
    """Zwift-style block chart, coloured by zone."""
    segs = segments(steps)
    total = sum(d for _, d, _, _ in segs) or 1
    top = max([1.3] + [max(a, b) for _, _, a, b in segs])
    parts = []
    for start, dur, a, b in segs:
        x0 = start / total * width
        x1 = (start + dur) / total * width
        ya = height - a / top * height
        yb = height - b / top * height
        colour = _zone_colour((a + b) / 2)
        parts.append(f'<polygon points="{x0:.1f},{height} {x0:.1f},{ya:.1f} {x1:.1f},{yb:.1f} {x1:.1f},{height}" '
                     f'fill="{colour}"/>')
    ftp_y = height - 1 / top * height
    parts.append(f'<line x1="0" x2="{width}" y1="{ftp_y:.1f}" y2="{ftp_y:.1f}" class="ftp-line"/>')
    return (f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" class="workout-svg" '
            f'role="img" aria-label="Workout profile">{"".join(parts)}</svg>')


def _zone_colour(p: float) -> str:
    # Zwift zone colours: grey, blue, green, yellow, orange, red
    for limit, colour in ((0.60, "#8c8c8c"), (0.76, "#3a8fd9"), (0.90, "#4caf50"),
                          (1.05, "#f2c94c"), (1.18, "#f2994a"), (9, "#eb5757")):
        if p < limit:
            return colour
    return "#eb5757"


CUE_GAP_S = 15  # Zwift shows a message for ~10 s; don't stack a generated cheer on top of the coach's cue
CHEER_POWER = 0.88  # sweetspot and up; openers, drills and Z2 repeats get a plain counter, not a pep talk
CHEER_MIN_ON_S = 60  # short reps (30/15s) would flash a message every rep: only mark the last one


def text_events(step: dict, cheers: tuple[str, ...] = (), last_rep: str = "") -> list[tuple[int, str]]:
    """(offset_s, message) for one step: the coach's cues, plus a rep counter at the start of every later
    interval rep the coach didn't already cue, with a cheer in the coach's voice on hard reps."""
    length = step_seconds(step)
    events = [(0, step["text"])] if step.get("text") else []
    events += [(c["offset_s"], c["text"]) for c in step.get("cues") or []
               if c.get("text") and 0 <= c["offset_s"] < length]
    if step["type"] == "intervals" and step["repeat"] > 1 and cheers:
        n, rep_s = step["repeat"], step["on_s"] + step["off_s"]
        hard = step["on_power"] >= CHEER_POWER
        for k in range(1, n):
            at = k * rep_s
            last = k == n - 1
            if (step["on_s"] < CHEER_MIN_ON_S and not last) or any(abs(t - at) < CUE_GAP_S for t, _ in events):
                continue
            line = (last_rep if last and last_rep else cheers[(k - 1) % len(cheers)]) if hard else ""
            events.append((at, f"Rep {k + 1}/{n}: {line}" if line else f"Rep {k + 1}/{n}"))
    return sorted(events, key=lambda e: e[0])


def to_zwo(name: str, description: str, steps: list[dict], cheers: tuple[str, ...] = (),
           last_rep: str = "") -> str:
    lines = [
        "<workout_file>",
        "  <author>Training Coach</author>",
        f"  <name>{_esc(name)}</name>",
        f"  <description>{_esc(description)}</description>",
        "  <sportType>bike</sportType>",
        "  <category>Training Coach</category>",
        "  <tags/>",
        "  <workout>",
    ]
    for s in steps:
        cad = f' Cadence="{s["cadence"]}"' if s.get("cadence") else ""
        t = s["type"]
        if t == "intervals":
            attrs = (f'Repeat="{s["repeat"]}" OnDuration="{s["on_s"]}" OffDuration="{s["off_s"]}" '
                     f'OnPower="{s["on_power"]:.2f}" OffPower="{s["off_power"]:.2f}"{cad}')
            tag = "IntervalsT"
        elif t == "freeride":
            attrs, tag = f'Duration="{s["duration_s"]}"', "FreeRide"
        elif t in ("warmup", "cooldown"):
            # Zwift ramps from PowerLow to PowerHigh for both tags; for cooldown, PowerLow is the start.
            tag = "Warmup" if t == "warmup" else "Cooldown"
            attrs = f'Duration="{s["duration_s"]}" PowerLow="{s["power_low"]:.2f}" PowerHigh="{s["power_high"]:.2f}"{cad}'
        elif abs(s["power_low"] - s["power_high"]) > 0.005:
            tag = "Ramp"
            attrs = f'Duration="{s["duration_s"]}" PowerLow="{s["power_low"]:.2f}" PowerHigh="{s["power_high"]:.2f}"{cad}'
        else:
            tag = "SteadyState"
            attrs = f'Duration="{s["duration_s"]}" Power="{s["power_low"]:.2f}"{cad}'
        events = text_events(s, cheers, last_rep)
        if events:
            lines.append(f"    <{tag} {attrs}>")
            lines += [f'      <textevent timeoffset="{t}" message={quoteattr(m)}/>' for t, m in events]
            lines.append(f"    </{tag}>")
        else:
            lines.append(f"    <{tag} {attrs}/>")
    lines += ["  </workout>", "</workout_file>", ""]
    return "\n".join(lines)


def filename(date_iso: str, title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40]
    return f"tc-{date_iso}-{slug}.zwo"


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
