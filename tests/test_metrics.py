from datetime import date, timedelta

import pytest

from app import metrics


def test_np_constant_power_equals_power():
    assert metrics.normalized_power([200.0] * 600) == pytest.approx(200.0)


def test_np_exceeds_average_for_variable_power():
    # 50/50 blocks of 100 W and 300 W, each 5 minutes: avg 200, NP well above
    watts = ([100.0] * 300 + [300.0] * 300) * 3
    np = metrics.normalized_power(watts)
    assert np > 230


def test_tss_one_hour_at_ftp_is_100():
    tss, intensity = metrics.tss_from_power(3600, 250, 250)
    assert tss == pytest.approx(100)
    assert intensity == pytest.approx(1.0)


def test_tss_half_hour_at_80pct():
    tss, _ = metrics.tss_from_power(1800, 200, 250)
    assert tss == pytest.approx(0.5 * 0.8 * 0.8 * 100)


def test_hr_tss():
    tss, intensity = metrics.tss_from_hr(3600, 150, 165)
    assert intensity == pytest.approx(150 / 165)
    assert tss == pytest.approx((150 / 165) ** 2 * 100)


def test_resample_fills_short_gaps_and_zeros_long():
    out = metrics.resample_1hz([0, 1, 3, 100], [100, 200, 300, 400])
    assert out[:4] == [100, 200, 200, 300]
    assert len(out) == 101
    assert out[50] == 0
    assert out[-1] == 400


def test_mmp_picks_best_window():
    watts = [100.0] * 600 + [350.0] * 300 + [100.0] * 600
    curve = metrics.mmp(watts)
    assert curve[300] == pytest.approx(350)
    assert curve[60] == pytest.approx(350)
    assert 3600 not in curve


def test_eftp_from_20min():
    assert metrics.eftp_from_mmp({1200: 300}) == 285
    assert metrics.eftp_from_mmp({1200: 300, 3600: 290}) == 290
    assert metrics.eftp_from_mmp({300: 400}) is None


def test_eftp_from_synthetic_stream():
    watts = [150.0] * 900 + [280.0] * 1200 + [150.0] * 900
    assert metrics.eftp_from_mmp(metrics.mmp(watts)) == 266


def test_critical_power_recovers_model():
    cp, wp = 260, 20000
    curve = {t: cp + wp / t for t in (180, 300, 600, 1200)}
    assert metrics.critical_power(curve) == (260, 20000)


def test_zone_seconds():
    hist = {175: 60, 250: 60, 400: 30}  # bin midpoints 187.5, 262.5, 412.5 with FTP 250
    z = metrics.zone_seconds(hist, 250)
    assert z[1] == 60   # 0.75 → Z2
    assert z[3] == 60   # 1.05 → Z4
    assert z[6] == 30   # 1.65 > 1.5 → Z7
    assert sum(z) == 150


def test_fitness_series_constant_load_converges():
    start = date(2026, 1, 1)
    daily = {start + timedelta(days=i): 50.0 for i in range(200)}
    series = metrics.fitness_series(daily, start + timedelta(days=199), start + timedelta(days=199))
    assert series[0]["ctl"] == pytest.approx(50, abs=1)
    assert series[0]["atl"] == pytest.approx(50, abs=0.1)


def test_fitness_series_first_day():
    d = date(2026, 1, 1)
    s = metrics.fitness_series({d: 42.0}, d, d + timedelta(days=1))
    assert s[0]["ctl"] == pytest.approx(1.0)
    assert s[0]["atl"] == pytest.approx(6.0)
    assert s[0]["tsb"] == 0
    assert s[1]["tsb"] == pytest.approx(-5.0)


def test_decoupling_detects_drift():
    watts = [200.0] * 3600
    hr = [130.0] * 1800 + [143.0] * 1800
    assert metrics.decoupling(watts, hr) == pytest.approx(9.1, abs=0.1)
    assert metrics.decoupling(watts[:600], hr[:600]) is None
