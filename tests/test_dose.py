from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from conftest import constant_day, load_json
from uv_dose.dose import (
    SED_PER_UVI_MINUTE,
    Source,
    build_series,
    hourly_breakdown,
    rate_dose,
    sun_protection_times,
    uv_category,
    window_dose,
)

T0 = datetime(2026, 1, 1, 10, 0)


def test_unit_conversion():
    # 1 UVI = 25 mW/m^2; 1 SED = 100 J/m^2 -> 0.015 SED per UVI-minute
    assert SED_PER_UVI_MINUTE == pytest.approx(0.015)


def test_constant_uvi_one_hour():
    series = build_series(constant_day(10.0, T0, 120))
    wd = window_dose(series, T0, T0 + timedelta(hours=1))
    assert wd.minutes == 60
    assert wd.sed == pytest.approx(9.0)  # 10 UVI * 0.015 * 60
    assert wd.joules_per_m2 == pytest.approx(900.0)
    assert wd.peak_uvi == 10.0 and wd.mean_uvi == pytest.approx(10.0)


@pytest.mark.parametrize(("source", "column"), [(Source.BEST, "CumulativeMeasured"), (Source.CLEAR_SKY, "CumulativeForecast")])
def test_matches_arpansa_dose_chart(mel_day, source, column):
    """ARPANSA's own UV-dose chart (whole SEDs) for the same day and city.

    ARPANSA labels each bar with the hour's start time but shows the cumulative
    dose at the *end* of that hour.
    """
    series = build_series(mel_day, source)
    day_start = datetime(2026, 11, 20, 6, 0)
    for row in load_json("uvdose_mel_2026-11-20.json"):
        hour = int(row["DoseTime"][:2])
        ours = window_dose(series, day_start, day_start.replace(hour=hour) + timedelta(hours=1)).sed
        assert ours == pytest.approx(row[column], abs=0.55), row["DoseTime"]


def test_partial_day_uses_forecast_after_latest_reading(mel_partial_day):
    latest = mel_partial_day.latest_measured
    assert latest is not None and latest.time == datetime(2026, 11, 21, 15, 17)
    assert not mel_partial_day.is_complete
    series = build_series(mel_partial_day)
    wd = window_dose(series, datetime(2026, 11, 21, 15, 0), datetime(2026, 11, 21, 16, 0))
    assert wd.forecast_minutes == 42  # 15:18 .. 15:59
    assert wd.missing_minutes == 0
    clear = build_series(mel_partial_day, Source.CLEAR_SKY)
    assert all(p.kind == "forecast" for p in clear)


def test_hourly_breakdown_partial_hours(mel_day):
    series = build_series(mel_day)
    start, end = datetime(2026, 11, 20, 9, 30), datetime(2026, 11, 20, 12, 10)
    rows = hourly_breakdown(series, start, end)
    assert [(r.start.strftime("%H:%M"), r.end.strftime("%H:%M")) for r in rows] == [
        ("09:30", "10:00"), ("10:00", "11:00"), ("11:00", "12:00"), ("12:00", "12:10"),
    ]
    assert [r.minutes for r in rows] == [30, 60, 60, 10]
    assert rows[-1].cumulative_sed == pytest.approx(window_dose(series, start, end).sed)


@pytest.mark.parametrize(
    ("uvi", "name"),
    [(0, "Low"), (2.49, "Low"), (2.5, "Moderate"), (5.4, "Moderate"), (5.5, "High"),
     (7.5, "Very high"), (10.49, "Very high"), (10.5, "Extreme"), (14, "Extreme")],
)
def test_uv_category(uvi, name):
    assert uv_category(uvi).name == name


@pytest.mark.parametrize(
    ("dose", "level"), [(0.0, "low"), (1.24, "low"), (1.25, "moderate"), (2.5, "high"), (4.99, "high"), (5.0, "very_high")]
)
def test_rate_dose(dose, level):
    assert rate_dose(dose, med_sed=2.5).level == level


def test_sun_protection_times(mel_day):
    prot = sun_protection_times(build_series(mel_day, Source.CLEAR_SKY))
    assert prot is not None
    # Melbourne, 20 Nov 2026, clear sky: UV 3+ from mid-morning to late afternoon
    assert datetime(2026, 11, 20, 8, 0) < prot.start < datetime(2026, 11, 20, 10, 0)
    assert datetime(2026, 11, 20, 15, 30) < prot.end < datetime(2026, 11, 20, 18, 0)


def test_sun_protection_times_threshold_and_none():
    assert sun_protection_times(build_series(constant_day(2.4, T0, 60))) is None  # rounds to 2
    prot = sun_protection_times(build_series(constant_day(2.5, T0, 60)))  # rounds to 3
    assert (prot.start, prot.end) == (T0, T0 + timedelta(minutes=60))
