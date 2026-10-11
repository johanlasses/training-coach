import xml.etree.ElementTree as ET

import pytest

from app import workouts


def step(type_, **kw):
    s = {"type": type_, "duration_s": 0, "power_low": 0, "power_high": 0, "repeat": 0, "on_s": 0,
         "off_s": 0, "on_power": 0, "off_power": 0, "cadence": 0, "text": ""}
    s.update(kw)
    return s


STEPS = [
    step("warmup", duration_s=600, power_low=0.45, power_high=0.75),
    step("intervals", repeat=4, on_s=300, off_s=180, on_power=1.05, off_power=0.55, cadence=95, text="Hold it <steady> & smooth"),
    step("steady", duration_s=600, power_low=0.65, power_high=0.65),
    step("steady", duration_s=120, power_low=0.6, power_high=0.8),
    step("freeride", duration_s=300),
    step("cooldown", duration_s=480, power_low=0.6, power_high=0.4),
]


def test_total_seconds():
    assert workouts.total_seconds(STEPS) == 600 + 4 * 480 + 600 + 120 + 300 + 480


def test_zwo_parses_and_durations_match():
    xml = workouts.to_zwo("Threshold 4x5", "Desc & more", STEPS)
    root = ET.fromstring(xml)
    assert root.find("name").text == "Threshold 4x5"
    assert root.find("sportType").text == "bike"
    els = list(root.find("workout"))
    assert [e.tag for e in els] == ["Warmup", "IntervalsT", "SteadyState", "Ramp", "FreeRide", "Cooldown"]
    total = 0
    for e in els:
        if e.tag == "IntervalsT":
            total += int(e.get("Repeat")) * (int(e.get("OnDuration")) + int(e.get("OffDuration")))
        else:
            total += int(e.get("Duration"))
    assert total == workouts.total_seconds(STEPS)
    iv = els[1]
    assert iv.get("OnPower") == "1.05" and iv.get("Cadence") == "95"
    assert iv.find("textevent").get("message") == "Hold it <steady> & smooth"
    assert els[5].get("PowerLow") == "0.60" and els[5].get("PowerHigh") == "0.40"


def test_estimated_tss_one_hour_at_ftp():
    assert workouts.estimated_tss([step("steady", duration_s=3600, power_low=1.0, power_high=1.0)]) == pytest.approx(100)


def test_svg_profile_is_valid_xml():
    ET.fromstring(workouts.svg_profile(STEPS))


def test_filename():
    assert workouts.filename("2026-10-12", "Sweet Spot 3×12!") == "tc-2026-10-12-sweet-spot-3-12.zwo"
