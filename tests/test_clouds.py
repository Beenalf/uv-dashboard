from __future__ import annotations

import math
from datetime import date, datetime, timedelta

import pytest

from conftest import MELBOURNE, constant_day, load_json
from uv_dose.arpansa import FALLBACK_SITES
from uv_dose.clouds import (
    FADE_MINUTES,
    FALLBACK_FADE_MINUTES,
    CloudError,
    CloudForecast,
    CloudOutlook,
    OpenMeteoClient,
    cloud_outlook,
    parse_forecast,
    recent_factor,
)
from uv_dose.dose import Source, build_series, window_dose
from uv_dose.planner import Outing, plan_day
from uv_dose.sun import solar_noon

PERTH = next(s for s in FALLBACK_SITES if s.code == "per")


def forecast_for(day: date, factor: float, site=MELBOURNE) -> CloudForecast:
    """A cloud forecast with the same factor for every daylight hour of ``day``."""
    return CloudForecast(site.code, tuple((datetime.combine(day, datetime.min.time()) + timedelta(hours=h, minutes=30),
                                           factor) for h in range(6, 20)))


# --------------------------------------------------------------------------- Open-Meteo forecast


def test_parse_real_forecast_in_local_time():
    """Open-Meteo's GMT hours become Melbourne daylight time (+11), each at the middle of the hour it covers."""
    fc = parse_forecast(load_json("openmeteo_mel_2026-11-25.json"), MELBOURNE)
    hours = dict(fc.hours)
    # "2026-11-26T03:00" GMT: UV 6.6 of 8.8 clear-sky over 02:00-03:00 GMT = 13:00-14:00 local.
    assert hours[datetime(2026, 11, 26, 13, 30)] == pytest.approx(6.6 / 8.8)
    assert all(0 <= f <= 1 for f in hours.values())
    assert all(5 <= t.hour <= 21 for t in hours)  # daylight only: night hours have too little UV for a ratio
    assert fc.covers(date(2026, 11, 26)) and not fc.covers(date(2026, 11, 30))


def test_each_value_covers_the_hour_before_its_time_stamp():
    """Why each value is placed half an hour before its time stamp: the clear-sky curve then peaks at
    solar noon. Placed at the time stamp itself, it would peak about half an hour late."""
    hourly = load_json("openmeteo_mel_2026-11-25.json")["hourly"]
    placed = [(datetime.fromisoformat(t) + timedelta(hours=11, minutes=-30), c)  # Melbourne daylight time
              for t, c in zip(hourly["time"], hourly["uv_index_clear_sky"])]
    assert {t for t, _ in parse_forecast({"hourly": hourly}, MELBOURNE).hours} <= {t for t, _ in placed}
    day = [(t, c) for t, c in placed if t.date() == date(2026, 11, 25)]
    i = max(range(1, len(day) - 1), key=lambda k: day[k][1])
    (t, b), a, c = day[i], day[i - 1][1], day[i + 1][1]
    peak = t + timedelta(hours=0.5 * (a - c) / (a - 2 * b + c))  # vertex of the parabola through three hours
    assert abs(peak - solar_noon(MELBOURNE, date(2026, 11, 25))) < timedelta(minutes=10)


def test_parse_converts_without_daylight_saving_and_caps_at_one():
    payload = {"hourly": {"time": ["2026-11-26T04:00", "2026-11-26T05:00", "2026-11-26T12:00"],
                          "uv_index": [9.0, 4.0, 0.0], "uv_index_clear_sky": [8.0, 8.0, 0.0]}}
    fc = parse_forecast(payload, PERTH)  # WA: UTC+8 all year
    assert fc.hours == ((datetime(2026, 11, 26, 11, 30), 1.0), (datetime(2026, 11, 26, 12, 30), 0.5))


@pytest.mark.parametrize("payload", [{}, {"hourly": {"time": []}}, None,
                                     {"hourly": {"time": ["2026-11-26T04:00"], "uv_index": [None],
                                                 "uv_index_clear_sky": [8.0]}}])
def test_parse_rejects_unusable_payloads(payload):
    with pytest.raises(CloudError):
        parse_forecast(payload, MELBOURNE)


def test_factor_between_hours_and_at_the_ends_of_the_day():
    fc = CloudForecast("mel", ((datetime(2026, 11, 26, 9, 30), 0.2), (datetime(2026, 11, 26, 10, 30), 0.6),
                               (datetime(2026, 11, 27, 9, 30), 0.9)))
    assert fc.factor_at(datetime(2026, 11, 26, 10, 0)) == pytest.approx(0.4)
    assert fc.factor_at(datetime(2026, 11, 26, 6, 0)) == 0.2  # held back to dawn
    assert fc.factor_at(datetime(2026, 11, 26, 19, 0)) == 0.6  # and on to dusk, not towards tomorrow
    assert fc.factor_at(datetime(2026, 11, 25, 12, 0)) is None


def test_client_caches_and_reports_bad_data(monkeypatch):
    client = OpenMeteoClient()
    calls = []

    def fake_get(params):
        calls.append(params)
        return load_json("openmeteo_mel_2026-11-25.json")

    monkeypatch.setattr(client, "_get_json", fake_get)
    first = client.forecast(MELBOURNE)
    assert client.forecast(MELBOURNE) is first and len(calls) == 1
    assert calls[0]["timezone"] == "GMT" and calls[0]["hourly"] == "uv_index,uv_index_clear_sky"
    monkeypatch.setattr(client, "_get_json", lambda params: {"oops": 1})
    with pytest.raises(CloudError):
        client.forecast(PERTH)


@pytest.mark.network
def test_live_forecast_covers_today_and_tomorrow():
    fc = OpenMeteoClient().forecast(MELBOURNE)
    assert fc.covers(date.today()) and fc.covers(date.today() + timedelta(days=1))


# --------------------------------------------------------------------------- the outlook


def test_recent_factor_is_the_last_hours_share_of_clear_sky(mel_partial_day):
    latest = mel_partial_day.latest_measured.time  # 15:17
    hour = [s for s in mel_partial_day.samples if latest - timedelta(minutes=59) <= s.time <= latest]
    expected = sum(s.measured for s in hour) / sum(s.forecast for s in hour)
    assert recent_factor(mel_partial_day) == pytest.approx(min(expected, 1.0))


def test_no_recent_factor_without_readings_or_with_a_low_sun():
    start = datetime(2026, 11, 26, 6, 0)
    assert recent_factor(constant_day(0.2, start, 120)) is None  # dawn: the ratio would be noise
    day = constant_day(4.0, start, 120)
    assert recent_factor(day) == pytest.approx(1.0)
    for s in day.samples:
        object.__setattr__(s, "measured", None)
    assert recent_factor(day) is None


def test_outlook_blends_recent_into_forecast():
    start = datetime(2026, 11, 26, 12, 0)
    o = CloudOutlook(start, 0.4, forecast_for(start.date(), 0.8))
    assert o.factor(start) == pytest.approx(0.4)
    w = math.exp(-1)
    assert o.factor(start + timedelta(minutes=FADE_MINUTES)) == pytest.approx(0.4 * w + 0.8 * (1 - w))
    assert o.factor(start + timedelta(hours=7)) == pytest.approx(0.8, abs=0.02)  # 3% recent left


def test_outlook_without_a_forecast_fades_back_to_clear_sky():
    start = datetime(2026, 11, 26, 12, 0)
    o = CloudOutlook(start, 0.4, None)
    w = math.exp(-1)
    assert o.factor(start + timedelta(minutes=FALLBACK_FADE_MINUTES)) == pytest.approx(0.4 * w + (1 - w))
    assert CloudOutlook(start, None, forecast_for(start.date(), 0.7)).factor(start) == pytest.approx(0.7)
    assert CloudOutlook(start, None, None).factor(start) == 1.0


def test_outlook_for_a_partial_day(mel_partial_day, mel_day):
    d = mel_partial_day.day
    o = cloud_outlook(mel_partial_day, forecast_for(d, 0.5))
    assert o.start == datetime(2026, 11, 21, 15, 18) and o.recent is not None and o.forecast is not None
    # Another site's or another day's forecast is ignored, leaving the recent factor.
    assert cloud_outlook(mel_partial_day, forecast_for(d, 0.5, site=PERTH)).forecast is None
    assert cloud_outlook(mel_partial_day, forecast_for(d + timedelta(days=1), 0.5)).forecast is None
    assert cloud_outlook(mel_day, forecast_for(mel_day.day, 0.5)) is None  # complete: nothing ahead


def test_series_uses_likely_uv_after_the_latest_reading(mel_partial_day):
    o = cloud_outlook(mel_partial_day, forecast_for(mel_partial_day.day, 0.5))
    series = build_series(mel_partial_day, Source.BEST, o)
    by_time = {s.time: s for s in mel_partial_day.samples}
    after = [p for p in series if p.time >= o.start]
    assert after and all(p.kind == "likely" for p in after if by_time[p.time].forecast is not None)
    p = next(p for p in after if p.time == datetime(2026, 11, 21, 17, 0))
    assert p.uvi == pytest.approx(by_time[p.time].forecast * o.factor(p.time))
    assert all(p.kind == "measured" for p in series if p.time < o.start)
    assert not any(p.kind == "likely" for p in build_series(mel_partial_day, Source.CLEAR_SKY, o))
    wd = window_dose(series, datetime(2026, 11, 21, 15, 0), datetime(2026, 11, 21, 16, 0))
    assert (wd.likely_minutes, wd.forecast_minutes) == (42, 0)


def test_cloud_makes_the_burn_later(mel_partial_day):
    o = cloud_outlook(mel_partial_day, forecast_for(mel_partial_day.day, 0.3))
    outing = [Outing(datetime(2026, 11, 21, 15, 30), datetime(2026, 11, 21, 17, 30))]
    likely = plan_day(build_series(mel_partial_day, Source.BEST, o), outing, med_sed=2.0)
    clear = plan_day(build_series(mel_partial_day, Source.BEST), outing, med_sed=2.0)
    assert likely.likely_minutes == 120 and clear.likely_minutes == 0
    assert likely.skin_sed < clear.skin_sed
    assert clear.burn and (likely.burn is None or likely.burn.at > clear.burn.at)


def test_burn_card_leads_with_the_time_if_it_clears(mel_partial_day):
    from uv_dose.gui.app import _burn_times

    o = cloud_outlook(mel_partial_day, forecast_for(mel_partial_day.day, 0.3))
    outing = [Outing(datetime(2026, 11, 21, 15, 30), datetime(2026, 11, 21, 17, 30))]
    likely = plan_day(build_series(mel_partial_day, Source.BEST, o), outing, med_sed=2.0)
    clear = plan_day(build_series(mel_partial_day, Source.BEST), outing, med_sed=2.0)
    assert _burn_times(likely, clear) == (clear.burn, likely.burn, True)
    assert _burn_times(clear, None) == (clear.burn, clear.burn, False)  # no cloud outlook: one time
    assert _burn_times(clear, clear)[2] is False  # the same time either way: shown once
