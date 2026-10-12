from datetime import date, timedelta

import pytest

from app import season


def minutes(hm: str) -> int:
    h, m = hm.split(":")
    return int(h) * 60 + int(m)


# Published Västerås sunrise/sunset (local clock time), rounded to the minute.
@pytest.mark.parametrize("day, sunrise, sunset", [
    ("2026-06-21", "03:35", "22:13"),
    ("2026-12-21", "08:55", "14:48"),
    ("2026-03-20", "06:04", "18:07"),
    ("2026-10-12", "07:35", "17:51"),
])
def test_sunrise_sunset_within_ten_minutes(day, sunrise, sunset):
    _, rise, set_ = season.daylight(date.fromisoformat(day))
    assert abs(minutes(rise) - minutes(sunrise)) <= 10
    assert abs(minutes(set_) - minutes(sunset)) <= 10


def test_autumn_sunset_gets_earlier_every_day():
    # Excludes the DST switch on the last Sunday of October, where clock time jumps back an hour.
    d = date(2026, 8, 1)
    prev = minutes(season.daylight(d)[2])
    while d < date(2026, 10, 24):
        d += timedelta(days=1)
        cur = minutes(season.daylight(d)[2])
        assert cur <= prev, f"sunset later on {d} than the day before"
        prev = cur


def test_hm_carries_rounded_minutes_into_the_hour():
    assert season._hm(18 + 59.6 / 60) == "19:00"
    assert season._hm(23.999) == "00:00"
    assert season._hm(7.5) == "07:30"
