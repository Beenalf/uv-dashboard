from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from conftest import constant_day
from uv_dose.dose import build_series
from uv_dose.planner import Outing, plan_day
from uv_dose.skin import MAX_SCORE, QUESTIONS, SKIN_TYPES, score_answers, skin_type_from_score
from uv_dose.vitamin_d import dose_for_vitamin_d, most_before_burning, next_goal, vitamin_d_iu

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
    assert vitamin_d_iu(effective_sed=0.625, med_sed=2.5, body_fraction=0.25) == pytest.approx(1000)
    assert dose_for_vitamin_d(1000, med_sed=2.5, body_fraction=0.25) == pytest.approx(0.625)


def test_vitamin_d_plateau_and_unreachable():
    # Production is capped at 1 MED, so a huge dose doesn't give unlimited IU
    assert vitamin_d_iu(100, 2.5, 0.25) == pytest.approx(4000)
    # 5000 IU on face & hands would need > 1 MED -> unreachable without burning
    assert dose_for_vitamin_d(5000, 2.5, 0.10) is None


# ---------------------------------------------------------------- planner


def test_plan_constant_sun():
    t0 = datetime(2026, 1, 1, 10, 0)
    series = build_series(constant_day(10.0, t0, 300))  # 0.15 SED/min
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


def test_most_before_burning_limits_high_goals():
    assert most_before_burning(0.25) == pytest.approx(4000)
    assert most_before_burning(0.85) == pytest.approx(13600)
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
