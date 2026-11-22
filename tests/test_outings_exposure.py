from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest

from conftest import MELBOURNE, constant_day
from uv_dose.arpansa import FALLBACK_SITES, DayData, Sample, clear_sky_outlook, observes_dst
from uv_dose.dose import build_series, window_dose
from uv_dose.exposure import APPLICATION_CHOICES, choice, effective_spf, exposure_factor
from uv_dose.gui.outings import OutingSpec
from uv_dose.planner import Outing, plan_curves, plan_day, plan_hours, trim_overlaps, value_at
from uv_dose.settings import Settings

T0 = datetime(2026, 1, 1, 8, 0)
BRISBANE = next(s for s in FALLBACK_SITES if s.code == "bri")


def at(h: int, m: int = 0) -> datetime:
    return T0.replace(hour=h, minute=m)


# ---------------------------------------------------------------- exposure factors


def test_effective_spf_scales_with_amount_applied():
    assert effective_spf(30, 1.0) == 30
    assert effective_spf(30, 0.5) == pytest.approx(15.5)
    assert effective_spf(50, 0.25) == pytest.approx(13.25)
    assert effective_spf(1, 0.5) == 1  # no sunscreen stays no sunscreen


def test_no_sunscreen_amount_turns_spf_off():
    none = choice(APPLICATION_CHOICES, "none")
    assert none.key == "none" and none.value == 0
    assert effective_spf(50, none.value) == 1
    spec = OutingSpec(datetime(2026, 1, 1, 9).time(), datetime(2026, 1, 1, 10).time(), "good", "sand", 30)
    assert spec.conditions() == "good shade · sand · SPF 30"
    assert spec.conditions(sunscreen=False) == "good shade · sand"


def test_exposure_factor():
    assert exposure_factor() == 1
    assert exposure_factor(shade=0.5) == 0.5
    assert exposure_factor(reflection=0.8) == pytest.approx(1.8)  # open snow
    assert exposure_factor(0.5, 0.15, 10) == pytest.approx(0.065)  # umbrella on sand, SPF 10 effective


# ---------------------------------------------------------------- outings


def test_trim_overlaps_sorts_and_counts_minutes_once():
    outings, trimmed = trim_overlaps([Outing(at(12), at(13)), Outing(at(9), at(10)), Outing(at(12, 30), at(14))])
    assert trimmed
    assert [(o.start, o.end) for o in outings] == [(at(9), at(10)), (at(12), at(13)), (at(13), at(14))]
    inside, _ = trim_overlaps([Outing(at(9), at(12)), Outing(at(10), at(11))])
    assert inside[1].duration == timedelta(0)
    assert trim_overlaps([Outing(at(9), at(10)), Outing(at(10), at(11))])[1] is False  # touching is fine


def test_doses_add_up_across_outings():
    series = build_series(constant_day(10.0, T0, 600))  # 0.15 SED/min, 08:00-18:00
    plan = plan_day(series, [Outing(at(9), at(9, 10)), Outing(at(12), at(12, 10), 0.5)], med_sed=100)
    assert plan.ambient_sed == pytest.approx(3.0)
    assert plan.skin_doses == pytest.approx((1.5, 0.75))
    assert plan.skin_sed == pytest.approx(2.25)
    assert plan.sun_time == timedelta(minutes=20)


def test_burn_counts_earlier_outings_and_only_time_outside():
    series = build_series(constant_day(10.0, T0, 600))
    # 10 min in the morning = 1.5 SED, so the 2.5 SED threshold comes 6 min 40 s into the lunch outing
    plan = plan_day(series, [Outing(at(9), at(9, 10)), Outing(at(12), at(13))], med_sed=2.5)
    assert plan.burn.outing == 1
    assert plan.burn.at == at(12) + timedelta(minutes=1.0 / 0.15)
    assert plan.burn.sun_time == timedelta(minutes=10) + timedelta(minutes=1.0 / 0.15)


def test_threshold_after_the_last_outing_is_in_the_tail():
    series = build_series(constant_day(10.0, T0, 600))
    plan = plan_day(series, [Outing(at(9), at(9, 5)), Outing(at(12), at(12, 5))], med_sed=2.5)
    # 1.5 SED from the two outings; the last 1 SED comes from staying out after 12:05
    assert plan.burn.outing is None
    assert plan.burn.at == at(12, 5) + timedelta(minutes=1.0 / 0.15)
    assert plan.dose_available == pytest.approx(1.5 + 0.15 * 355)  # outings, then 12:05-18:00


def test_plan_curves_are_flat_between_outings():
    series = build_series(constant_day(10.0, T0, 600))
    outings = [Outing(at(9), at(9, 10)), Outing(at(12), at(12, 10))]
    curve, tail = plan_curves(series, outings)
    assert value_at(curve, at(8)) is None
    assert value_at(curve, at(9, 10)) == pytest.approx(1.5)
    assert value_at(curve, at(11)) == pytest.approx(1.5)  # between outings
    assert value_at(curve, at(12, 10)) == pytest.approx(3.0)
    assert tail[0][0] == at(12, 10) and tail[1][0] == pytest.approx(3.0)
    assert tail[0][-1] == at(18) and tail[1][-1] == pytest.approx(3.0 + 0.15 * 350)


def test_single_outing_curve_matches_window_dose(mel_day):
    series = build_series(mel_day)
    start, end = datetime(2026, 11, 20, 9, 30), datetime(2026, 11, 20, 14, 15)
    (times, values), _ = plan_curves(series, [Outing(start, end)])
    assert times[0] == start and values[0] == 0
    assert times[-1] == end
    assert values[-1] == pytest.approx(window_dose(series, start, end).sed)


def test_plan_hours_run_across_outings():
    series = build_series(constant_day(10.0, T0, 600))
    rows = plan_hours(series, [Outing(at(9, 30), at(10, 30)), Outing(at(14), at(14, 20), 0.5)])
    assert [(r.outing, f"{r.start:%H:%M}", f"{r.end:%H:%M}") for r in rows] == [
        (0, "09:30", "10:00"), (0, "10:00", "10:30"), (1, "14:00", "14:20")]
    assert rows[-1].cumulative_sed == pytest.approx(0.15 * 80)
    assert rows[-1].skin_cumulative_sed == pytest.approx(0.15 * 60 + 0.15 * 20 * 0.5)


# ---------------------------------------------------------------- outlook for tomorrow


def _day(site, d: date) -> DayData:
    start = datetime.combine(d, datetime.min.time()).replace(hour=6)
    samples = [Sample(start + timedelta(minutes=i), 1.0 + i / 1000, 2.0) for i in range(840)]
    return DayData(site=site, day=d, samples=samples)


def test_outlook_moves_the_forecast_to_tomorrow_and_drops_measurements():
    base = _day(MELBOURNE, date(2026, 11, 21))
    out = clear_sky_outlook(base, date(2026, 11, 22))
    assert out.day == date(2026, 11, 22) and out.outlook_from == date(2026, 11, 21)
    assert out.first_time == datetime(2026, 11, 22, 6, 0)
    assert not out.has_measurements and out.has_data
    assert all(s.forecast == 2.0 for s in out.samples)


@pytest.mark.parametrize(("base", "target", "shift_hours"), [
    (date(2026, 10, 3), date(2026, 10, 4), 1),  # daylight saving starts (first Sunday in October)
    (date(2026, 4, 4), date(2026, 4, 5), -1),  # and ends (first Sunday in April)
    (date(2026, 11, 21), date(2026, 11, 22), 0),
])
def test_outlook_follows_daylight_saving(base, target, shift_hours):
    out = clear_sky_outlook(_day(MELBOURNE, base), target)
    assert out.first_time == datetime.combine(target, datetime.min.time()).replace(hour=6 + shift_hours)
    # Queensland has no daylight saving
    assert clear_sky_outlook(_day(BRISBANE, base), target).first_time.hour == 6


def test_observes_dst():
    assert observes_dst(MELBOURNE, date(2026, 1, 15))
    assert not observes_dst(MELBOURNE, date(2026, 6, 15))
    assert not observes_dst(BRISBANE, date(2026, 1, 15))


# ---------------------------------------------------------------- settings


def test_old_settings_become_one_outing(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"start": "09:00", "end": "11:30", "spf": 30, "site_code": "syd"}), encoding="utf-8")
    s = Settings.load(path)
    assert s.site_code == "syd"
    assert s.outings == [{"start": "09:00", "end": "11:30", "spf": 30}]
    s.save(path)
    assert "start" not in json.loads(path.read_text(encoding="utf-8"))
