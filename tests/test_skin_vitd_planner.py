from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from conftest import constant_day
from uv_dose.dose import build_series
from uv_dose.planner import plan_exposure
from uv_dose.skin import MAX_SCORE, QUESTIONS, SKIN_TYPES, score_answers, skin_type_from_score
from uv_dose.vitamin_d import dose_for_vitamin_d, vitamin_d_iu

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
    plan = plan_exposure(series, t0, med_sed=2.5, target_iu=1000, body_fraction=0.25)
    assert plan.burn_after == timedelta(minutes=2.5 / 0.15)  # 16 min 40 s
    assert plan.vitd_after == timedelta(minutes=0.625 / 0.15)  # ~4 min 10 s
    assert plan.vitd_at < plan.burn_at
    assert not plan.relies_on_forecast

    with_spf = plan_exposure(series, t0, med_sed=2.5, spf=30, target_iu=1000, body_fraction=0.25)
    assert with_spf.burn_at is None  # only 300 min of data -> 1.5 SED effective
    assert with_spf.dose_available == pytest.approx(1.5)


def test_plan_on_real_partial_day_flags_forecast(mel_partial_day):
    series = build_series(mel_partial_day)
    plan = plan_exposure(series, datetime(2026, 11, 21, 15, 0), med_sed=2.5)
    assert plan.burn_at is not None
    assert plan.relies_on_forecast  # measurements end at 15:17
