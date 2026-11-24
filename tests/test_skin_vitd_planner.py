from __future__ import annotations

import math
from dataclasses import replace
from datetime import date, datetime, time, timedelta

import pytest

from conftest import MELBOURNE, constant_day
from uv_dose.dose import build_series
from uv_dose.planner import Outing, plan_day
from uv_dose.skin import MAX_SCORE, QUESTIONS, SKIN_TYPES, score_answers, skin_type_from_score
from uv_dose.sun import cos_zenith, solar_noon
from uv_dose.vitamin_d import (
    body_exposure,
    dose_for_vitamin_d,
    iu_range,
    most_in_a_day,
    next_goal,
    round_iu,
    vitamin_d_factor,
    vitamin_d_iu,
)


def with_factor(series, factor):
    """The series with every minute's vitamin D factor set (1: vitamin D dose = sunburn dose)."""
    return [replace(p, vitd_factor=factor) for p in series]

# ---------------------------------------------------------------- Fitzpatrick


def test_questionnaire_shape():
    assert len(QUESTIONS) == 10 and MAX_SCORE == 40
    assert all(len(q.options) == 5 for q in QUESTIONS)
    assert len({q.key for q in QUESTIONS}) == 10


@pytest.mark.parametrize(
    ("score", "roman"),
    [(0, "I"), (6, "I"), (7, "II"), (13, "II"), (14, "III"), (20, "III"),
     (21, "IV"), (27, "IV"), (28, "V"), (34, "V"), (35, "VI"), (40, "VI")],
)
def test_score_bands_match_arpansa_sheet(score, roman):
    assert skin_type_from_score(score).roman == roman


def test_score_answers():
    answers = {q.key: 1 for q in QUESTIONS}
    assert score_answers(answers) == 10
    assert skin_type_from_score(score_answers(answers)).roman == "II"
    with pytest.raises(ValueError):
        score_answers({"eyes": 1})
    with pytest.raises(ValueError):
        score_answers({**answers, "eyes": 5})


def test_burn_thresholds_increase_with_type():
    meds = [t.med_sed for t in SKIN_TYPES]
    assert meds == sorted(meds) and meds[0] == 2.0


# ---------------------------------------------------------------- vitamin D


def test_holick_rule_of_thumb():
    # 1/4 MED on 1/4 of the body ~ 1000 IU
    assert vitamin_d_iu(vitd_sed=0.625, med_sed=2.5, body_fraction=0.25) == pytest.approx(1000)
    assert dose_for_vitamin_d(1000, med_sed=2.5, body_fraction=0.25) == pytest.approx(0.625)


def test_vitamin_d_plateau_and_unreachable():
    # Production is capped at 1 MED, so a huge dose doesn't give unlimited IU
    assert vitamin_d_iu(100, 2.5, 0.25) == pytest.approx(4000)
    # 5000 IU on face & hands would need > 1 MED -> unreachable without burning
    assert dose_for_vitamin_d(5000, 2.5, 0.10) is None


# ---------------------------------------------------------------- planner


def test_plan_constant_sun():
    t0 = datetime(2026, 1, 1, 10, 0)
    series = with_factor(build_series(constant_day(10.0, t0, 300)), 1.0)  # 0.15 SED/min
    plan = plan_day(series, [Outing(t0, t0 + timedelta(hours=1))], med_sed=2.5, target_iu=1000, body_fraction=0.25)
    assert plan.burn.sun_time == timedelta(minutes=2.5 / 0.15)  # 16 min 40 s
    assert plan.burn.at == t0 + plan.burn.sun_time and plan.burn.outing == 0
    assert plan.vitd.sun_time == timedelta(minutes=0.625 / 0.15)  # ~4 min 10 s
    assert plan.vitd.at < plan.burn.at
    assert not plan.relies_on_forecast

    # SPF 30 slows accumulation 30-fold: only 300 min of data -> 1.5 SED, so no burn
    with_spf = plan_day(series, [Outing(t0, t0 + timedelta(hours=1), 1 / 30)], med_sed=2.5)
    assert with_spf.burn is None
    assert with_spf.dose_available == pytest.approx(1.5)


def test_plan_interpolates_within_the_minute():
    t0 = datetime(2026, 1, 1, 10, 0)
    series = build_series(constant_day(10.0, t0, 240))  # 0.15 SED/min
    plan = plan_day(series, [Outing(t0, t0 + timedelta(minutes=5))], med_sed=0.075)
    assert plan.burn.at == t0 + timedelta(seconds=30)
    # SPF 10 (factor 0.1): 1 SED needs 66 min 40 s, past the 5-minute window, so it's in the tail
    tail = plan_day(series, [Outing(t0, t0 + timedelta(minutes=5), 0.1)], med_sed=1.0)
    assert tail.burn.at == t0 + timedelta(minutes=66, seconds=40)
    assert tail.burn.outing is None


def test_plan_on_real_partial_day_flags_forecast(mel_partial_day):
    series = build_series(mel_partial_day)
    t = datetime(2026, 11, 21, 15, 0)
    plan = plan_day(series, [Outing(t, t + timedelta(minutes=10))], med_sed=2.5)
    assert plan.burn is not None
    assert plan.relies_on_forecast  # measurements end at 15:17


def test_most_in_a_day_limits_high_goals():
    assert most_in_a_day(0.25) == pytest.approx(4000)
    assert most_in_a_day(0.85) == pytest.approx(13600)
    assert most_in_a_day(body_exposure("nude").fraction) == pytest.approx(16000)  # the whole body
    # 10,000 IU is out of reach with face, arms and hands, but not in swimwear
    assert dose_for_vitamin_d(10_000, 2.5, 0.25) is None
    assert dose_for_vitamin_d(10_000, 2.5, 0.85) == pytest.approx(10_000 / 13_600 * 2.5)


def test_goal_steps_from_1000_to_10000():
    assert next_goal(1000, +1) == 1500
    assert next_goal(1200, +1) == 1500  # a typed value goes to the next step
    assert next_goal(1200, -1) == 1000
    assert next_goal(7500, +1) == 10000
    assert next_goal(10000, +1) == 10000  # stays at the ends
    assert next_goal(1000, -1) == 1000
    assert next_goal(600, -1) == 600 and next_goal(600, +1) == 1000


def test_most_possible_is_a_range_across_people():
    # 25% of skin: 4,000 IU in the middle, 2,500-6,250 across the quoted 10,000-25,000 IU per whole-body MED
    assert iu_range(most_in_a_day(0.25)) == pytest.approx((2500, 6250))
    assert iu_range(most_in_a_day(0.85)) == pytest.approx((8500, 21250))


def test_round_iu_to_two_significant_figures():
    assert [round_iu(x) for x in (6250, 21250, 2500, 8500, 625, 13600, 0)] == [6300, 21000, 2500, 8500, 630, 14000, 0]


# ---------------------------------------------------------------- sun angle and vitamin D weighting


def test_solar_noon_matches_arpansa_forecast_peak(mel_day):
    """The clear-sky forecast peaks at solar noon: checks the time zone and daylight saving handling."""
    peak = max(mel_day.samples, key=lambda s: s.forecast or 0)
    noon = solar_noon(mel_day.site, mel_day.day)
    assert abs(peak.time + timedelta(seconds=30) - noon) < timedelta(minutes=1)
    assert noon.strftime("%H:%M") == "13:05"  # daylight saving time


def test_sun_angle_at_the_solstices():
    # At noon the sun is |latitude - declination| from overhead; declination is -23.44 / +23.44 degrees.
    for day, declination in ((date(2026, 12, 21), -23.44), (date(2026, 6, 21), 23.44)):
        mu = cos_zenith(MELBOURNE, day, [solar_noon(MELBOURNE, day) - timedelta(seconds=30)])[0]
        assert math.degrees(math.acos(mu)) == pytest.approx(abs(MELBOURNE.latitude - declination), abs=0.3)


def test_vitamin_d_factor_matches_pope_2008():
    """Single-time values from Pope et al. (2008); 1 is their reference sun."""
    for zenith, ozone, expected in ((35.2, 375, 1.00), (21.8, 325, 1.15), (43.3, 325, 1.01), (66.3, 375, 0.57)):
        assert vitamin_d_factor(math.cos(math.radians(zenith)), ozone) == pytest.approx(expected, abs=0.03)


def test_vitamin_d_factor_falls_with_lower_sun_and_more_ozone():
    factors = [vitamin_d_factor(math.cos(math.radians(z))) for z in range(0, 91, 10)]
    assert factors == sorted(factors, reverse=True)
    assert factors[-1] > 0
    mu = math.cos(math.radians(40))
    assert vitamin_d_factor(mu, 350) < vitamin_d_factor(mu, 300) < vitamin_d_factor(mu, 250)


def test_real_day_factors_follow_the_sun(mel_day):
    by_time = {p.time.time(): p.vitd_factor for p in build_series(mel_day)}
    noon = by_time[time(13, 5)]
    assert noon == pytest.approx(1.19, abs=0.02)  # sun 18 degrees from overhead
    assert by_time[time(8, 0)] < 0.7 * noon and by_time[time(18, 0)] < 0.7 * noon


def test_vitamin_d_follows_the_weighted_dose():
    t0 = datetime(2026, 1, 1, 10, 0)
    base = build_series(constant_day(10.0, t0, 300))  # 0.15 SED/min of sunburn dose
    outing = [Outing(t0, t0 + timedelta(hours=1))]

    low = plan_day(with_factor(base, 0.5), outing, med_sed=2.5, target_iu=1000, body_fraction=0.25)
    assert low.burn.sun_time == timedelta(minutes=2.5 / 0.15)  # burning doesn't change
    assert low.vitd.sun_time == timedelta(minutes=0.625 / 0.075)  # vitamin D takes twice as long
    # Production would level off at 2.5 SED of vitamin D dose (33 min), after the burn (16 min 40 s):
    # the most before burning is half the plateau.
    assert low.limit.kind == "burn" and low.limit.at == low.burn.at
    assert low.most_iu == pytest.approx(most_in_a_day(0.25) / 2)
    assert low.vitd_iu == pytest.approx(most_in_a_day(0.25))  # an hour out reaches the plateau anyway
    late = plan_day(with_factor(base, 0.5), outing, med_sed=2.5, target_iu=3000, body_fraction=0.25)
    assert late.vitd is not None and late.vitd_after_burn

    high = plan_day(with_factor(base, 1.25), outing, med_sed=2.5, target_iu=1000, body_fraction=0.25)
    assert high.limit.kind == "levels off" and high.limit.at < high.burn.at
    assert high.most_iu == pytest.approx(most_in_a_day(0.25))
    assert not high.vitd_after_burn

    short = plan_day(with_factor(base, 0.5), outing, med_sed=100, target_iu=1000, body_fraction=0.25)
    assert short.limit.kind == "day"  # 300 min x 0.075 = 22.5 SED: no plateau, no burn
    assert short.most_iu == pytest.approx(vitamin_d_iu(22.5, 100, 0.25))
