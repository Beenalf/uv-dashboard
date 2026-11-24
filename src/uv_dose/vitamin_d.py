"""Rough vitamin D estimate from UV dose.

Model (Holick's rule of thumb, used by Webb & Engelsen 2006 for their
"standard vitamin D dose"): exposing about a quarter of the body (face, hands
and arms) to a quarter of a minimal erythemal dose (MED) makes roughly
1000 IU of vitamin D. That implies about 16,000 IU per MED of whole-body
exposure, which sits inside the 10,000-25,000 IU often quoted for 1 MED.

    IU ~ 16,000 x (fraction of skin exposed) x min(vitamin D dose / MED, 1)

Those figures come from UV lamps, whose light makes vitamin D and sunburn in a
fixed proportion. Sunlight doesn't: vitamin D needs the shortest UVB, which ozone
and the air filter out more when the sun is low, while sunburn also comes from
longer wavelengths. So the "vitamin D dose" is the sunburn-weighted (erythemal)
dose with each minute multiplied by ``vitamin_d_factor``: the ratio of vitamin
D-weighted to sunburn-weighted UV at that minute's sun angle, scaled so 1 matches
the lamps (Pope et al. 2008). It is about 1.2 with the sun high, 1 at 35 degrees from
overhead, and falls fast as the sun gets lower: early, late and in winter.

People differ in how much vitamin D a dose makes (age, skin, body size), so the
most possible is also given as a range: one MED over the whole body is often
quoted as making 10,000-25,000 IU (Holick). The time it takes to reach the
plateau doesn't depend on that, only on the UV and the burn threshold.

Expressing dose in MEDs roughly accounts for skin type, because melanin
slows vitamin D production and burning alike. Production levels off as
exposure continues (previtamin D reaches a photo-equilibrium), so the
estimate is capped at 1 MED of vitamin D dose. This is a coarse public-health
approximation. It ignores age, body fat, diet and the body's shape (the doses
are for a flat surface facing the sky).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

IU_PER_MED_WHOLE_BODY = 16_000.0
IU_PER_MED_RANGE: tuple[float, float] = (10_000.0, 25_000.0)  # across people, for the same dose
PLATEAU_MED = 1.0
DEFAULT_TARGET_IU = 1000
# Steps for the goal's arrows. Any other value can be typed in.
GOAL_STEPS_IU: tuple[int, ...] = (1000, 1500, 2000, 3000, 4000, 5000, 7500, 10000)


@dataclass(frozen=True)
class BodyExposure:
    key: str
    label: str
    fraction: float  # of body surface area


# Approximate fractions from the "rule of nines" body surface chart.
BODY_EXPOSURES: tuple[BodyExposure, ...] = (
    BodyExposure("face_hands", "Face and hands (~10%)", 0.10),
    BodyExposure("face_arms", "Face, arms and hands (~25%)", 0.25),
    BodyExposure("tshirt_shorts", "T-shirt and shorts (~40%)", 0.40),
    BodyExposure("swimwear", "Swimwear (~85%)", 0.85),
    BodyExposure("nude", "Nude (100%)", 1.0),
)
DEFAULT_BODY_EXPOSURE = "face_arms"


# ---------------------------------------------------------------- vitamin D vs sunburn UV
# Pope, Holick, Mackin & Godar (2008), "Action spectrum conversion factors that change
# erythemally weighted to previtamin D3-weighted UV doses", Photochem. Photobiol. 84,
# doi:10.1111/j.1751-1097.2008.00373.x. Their factor is (D_eff / E_eff) / 1.51, where 1.51 is
# the ratio for the lamps used in the human studies; it is 1 at solar noon in mid-April at
# 45 N with 375 DU of ozone (sun 35.2 degrees from overhead).
#
# They tabulate day averages, so this per-minute formula was fitted to them: their Fig. 6
# (30-60 N, four seasons, 260 and 375 DU, averaged over the day weighted by sunburn UV) to
# an RMS error of 0.018 (worst 0.05, high sun and low ozone), and their single-time values
# (21.8 degrees, 325 DU: 1.15; 43.3, 325: 1.01; 66.3, 375: 0.57) to within 0.02.
#
#     m = (1 + c) / (cos(angle) + c) - 1     extra air the light crosses, vs an overhead sun
#     factor = exp(a - (e + h m) (ozone - 300) - g m)
_ASCF_A, _ASCF_E, _ASCF_H, _ASCF_G, _ASCF_C = 0.2125, 0.000947, 0.00234, 0.642, 0.261
# ARPANSA doesn't publish ozone. Over Australia it is mostly 250-350 DU through the year;
# each 50 DU either way moves the factor by about 5% (more when the sun is low).
OZONE_DU = 300.0


def vitamin_d_factor(cos_zenith: float, ozone_du: float = OZONE_DU) -> float:
    """How much vitamin D a unit of sunburn-weighted UV makes, relative to the lamps behind the IU figures.

    ``cos_zenith`` is the cosine of the sun's angle from overhead (see ``uv_dose.sun``).
    """
    m = (1 + _ASCF_C) / (max(cos_zenith, 0.0) + _ASCF_C) - 1
    return math.exp(_ASCF_A - (_ASCF_E + _ASCF_H * m) * (ozone_du - 300.0) - _ASCF_G * m)


def body_exposure(key: str) -> BodyExposure:
    for b in BODY_EXPOSURES:
        if b.key == key:
            return b
    raise KeyError(key)


def vitamin_d_iu(vitd_sed: float, med_sed: float, body_fraction: float) -> float:
    """Estimated IU made by a vitamin D dose on the skin (after sunscreen etc., weighted by vitamin_d_factor)."""
    if med_sed <= 0 or body_fraction <= 0 or vitd_sed <= 0:
        return 0.0
    meds = min(vitd_sed / med_sed, PLATEAU_MED)
    return IU_PER_MED_WHOLE_BODY * body_fraction * meds


def dose_for_vitamin_d(target_iu: float, med_sed: float, body_fraction: float) -> float | None:
    """Vitamin D dose (SED, weighted by vitamin_d_factor) needed to make ``target_iu``.

    Returns None when the target is at or above the plateau: more than that much
    skin can make in a day.
    """
    if target_iu <= 0:
        return 0.0
    if med_sed <= 0 or body_fraction <= 0:
        return None
    meds = target_iu / (IU_PER_MED_WHOLE_BODY * body_fraction)
    if meds >= PLATEAU_MED:
        return None
    return meds * med_sed


def most_in_a_day(body_fraction: float) -> float:
    """The most vitamin D (IU) a day can make with this much skin: production levels off after that."""
    return IU_PER_MED_WHOLE_BODY * max(body_fraction, 0.0) * PLATEAU_MED


def iu_range(central_iu: float) -> tuple[float, float]:
    """The spread across people around a central estimate, using IU_PER_MED_RANGE."""
    lo, hi = IU_PER_MED_RANGE
    return central_iu * lo / IU_PER_MED_WHOLE_BODY, central_iu * hi / IU_PER_MED_WHOLE_BODY


def round_iu(iu: float) -> int:
    """Round to two significant figures (6,250 -> 6,300), so ranges don't look more precise than they are."""
    if iu <= 0:
        return 0
    step = 10 ** max(len(str(int(iu))) - 2, 0)
    return int(iu / step + 0.5) * step


def next_goal(current: float, direction: int) -> float:
    """The next goal step above (direction > 0) or below ``current``, or ``current`` at either end."""
    if direction > 0:
        return next((g for g in GOAL_STEPS_IU if g > current), current)
    return next((g for g in reversed(GOAL_STEPS_IU) if g < current), current)
