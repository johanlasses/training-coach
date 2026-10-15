"""Training load math: NP, TSS, power curve, eFTP, CTL/ATL/TSB.

Pure functions, no I/O. Streams are 1 Hz lists (see resample_1hz).
"""
from __future__ import annotations

from datetime import date, timedelta

MMP_DURATIONS = [5, 15, 30, 60, 120, 300, 480, 600, 1200, 1800, 3600]  # 480 s: the CTS field test
HIST_BIN_W = 25
CTL_DAYS = 42
ATL_DAYS = 7

# Coggan power zones as fractions of FTP (upper bounds); Z7 is open-ended.
ZONES = [
    ("Z1 Recovery", 0.55),
    ("Z2 Endurance", 0.75),
    ("Z3 Tempo", 0.90),
    ("Z4 Threshold", 1.05),
    ("Z5 VO2max", 1.20),
    ("Z6 Anaerobic", 1.50),
    ("Z7 Neuromuscular", float("inf")),
]


def resample_1hz(times: list[int], values: list[float | None], max_gap: int = 30) -> list[float]:
    """Resample a Strava stream to 1 Hz. Short gaps hold the last value; long gaps (stops) are zeros."""
    if not times:
        return []
    out: list[float] = []
    for i, t in enumerate(times):
        v = float(values[i] or 0)
        if i:
            gap = t - times[i - 1]
            if gap > 1:
                fill = out[-1] if gap <= max_gap else 0.0
                out.extend([fill] * (gap - 1))
        out.append(v)
    return out


def normalized_power(watts: list[float]) -> float | None:
    """Coggan NP: 4th-power mean of the 30 s rolling average."""
    if len(watts) < 30:
        return None
    window = 30
    s = sum(watts[:window])
    acc = (s / window) ** 4
    n = 1
    for i in range(window, len(watts)):
        s += watts[i] - watts[i - window]
        acc += (s / window) ** 4
        n += 1
    return (acc / n) ** 0.25


def tss_from_power(seconds: float, np: float, ftp: float) -> tuple[float, float]:
    """Returns (TSS, IF)."""
    intensity = np / ftp
    return seconds * np * intensity / (ftp * 3600) * 100, intensity


def tss_from_hr(seconds: float, avg_hr: float, lthr: float) -> tuple[float, float]:
    """hrTSS approximation: hours * (avgHR/LTHR)^2 * 100."""
    intensity = avg_hr / lthr
    return seconds / 3600 * intensity**2 * 100, intensity


def estimate_tss(seconds: float) -> float:
    """No power, no HR: assume an endurance ride (~IF 0.7 → 49 TSS/h)."""
    return seconds / 3600 * 49


def mmp(watts: list[float], durations: list[int] = MMP_DURATIONS) -> dict[int, float]:
    """Mean-maximal power for each duration that fits in the ride."""
    prefix = [0.0]
    for w in watts:
        prefix.append(prefix[-1] + w)
    out = {}
    n = len(watts)
    for d in durations:
        if d > n:
            break
        best = max(prefix[i + d] - prefix[i] for i in range(n - d + 1))
        out[d] = round(best / d, 1)
    return out


def power_histogram(watts: list[float]) -> dict[int, int]:
    hist: dict[int, int] = {}
    for w in watts:
        b = int(w // HIST_BIN_W) * HIST_BIN_W
        hist[b] = hist.get(b, 0) + 1
    return hist


def zone_seconds(hist: dict[int, int], ftp: float) -> list[int]:
    """Seconds per Coggan zone, from a 25 W histogram (bin midpoint decides the zone)."""
    out = [0] * len(ZONES)
    for b, secs in hist.items():
        frac = (b + HIST_BIN_W / 2) / ftp
        for i, (_, upper) in enumerate(ZONES):
            if frac <= upper:
                out[i] += secs
                break
    return out


def decoupling(watts: list[float], hr: list[float]) -> float | None:
    """Pw:HR decoupling % (first vs second half efficiency factor). Only meaningful on steady rides."""
    n = min(len(watts), len(hr))
    if n < 45 * 60:
        return None
    half = n // 2

    def ef(w: list[float], h: list[float]) -> float | None:
        pairs = [(a, b) for a, b in zip(w, h) if b > 0]
        if not pairs:
            return None
        return (sum(a for a, _ in pairs) / len(pairs)) / (sum(b for _, b in pairs) / len(pairs))

    ef1 = ef(watts[:half], hr[:half])
    ef2 = ef(watts[half:n], hr[half:n])
    if not ef1 or not ef2:
        return None
    return round((ef1 - ef2) / ef1 * 100, 1)


def downsample(values: list[float], points: int = 120) -> list[float]:
    if not values:
        return []
    if len(values) <= points:
        return [round(v) for v in values]
    step = len(values) / points
    return [round(sum(values[int(i * step):int((i + 1) * step)]) / max(1, int((i + 1) * step) - int(i * step)))
            for i in range(points)]


def eftp_from_mmp(curve: dict[int, float]) -> float | None:
    """eFTP from a best-efforts curve: max(95% of 20 min, 60 min). None without long efforts."""
    candidates = []
    if 1200 in curve:
        candidates.append(curve[1200] * 0.95)
    if 3600 in curve:
        candidates.append(curve[3600])
    return round(max(candidates)) if candidates else None


def best_curve(curves: list[dict[int, float]]) -> dict[int, float]:
    out: dict[int, float] = {}
    for c in curves:
        for d, w in c.items():
            if w > out.get(d, 0):
                out[d] = w
    return dict(sorted(out.items()))


def critical_power(curve: dict[int, float]) -> tuple[float, float] | None:
    """2-parameter CP model fit (work = CP*t + W') over 3–20 min efforts. Returns (CP, W' in J)."""
    pts = [(d, w * d) for d, w in curve.items() if 120 <= d <= 1200]
    if len(pts) < 3:
        return None
    n = len(pts)
    mt = sum(t for t, _ in pts) / n
    mw = sum(w for _, w in pts) / n
    var = sum((t - mt) ** 2 for t, _ in pts)
    if var == 0:
        return None
    cp = sum((t - mt) * (w - mw) for t, w in pts) / var
    wprime = mw - cp * mt
    if cp <= 0 or wprime <= 0:
        return None
    return round(cp), round(wprime)


def daily_tss(activities: list[dict]) -> dict[date, float]:
    out: dict[date, float] = {}
    for a in activities:
        if a.get("tss") is None:
            continue
        d = date.fromisoformat(a["start_date_local"][:10])
        out[d] = out.get(d, 0.0) + a["tss"]
    return out


def fitness_series(daily: dict[date, float], start: date, end: date) -> list[dict]:
    """CTL/ATL/TSB per day. TSB(d) = CTL(d-1) - ATL(d-1), i.e. form going into day d.

    The series is warmed up from the first day with load, so `start` can be later.
    """
    first = min(daily) if daily else start
    first = min(first, start)
    ctl = atl = 0.0
    out = []
    d = first
    while d <= end:
        tsb = ctl - atl
        load = daily.get(d, 0.0)
        ctl += (load - ctl) / CTL_DAYS
        atl += (load - atl) / ATL_DAYS
        if d >= start:
            out.append({"date": d.isoformat(), "tss": round(load), "ctl": round(ctl, 1),
                        "atl": round(atl, 1), "tsb": round(tsb, 1)})
        d += timedelta(days=1)
    return out
