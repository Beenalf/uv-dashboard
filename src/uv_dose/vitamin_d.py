"""Rough vitamin D estimate from erythemal UV dose.

Model (Holick's rule of thumb, used by Webb & Engelsen 2006 for their
"standard vitamin D dose"): exposing about a quarter of the body (face, hands
and arms) to a quarter of a minimal erythemal dose (MED) makes roughly
1000 IU of vitamin D. That implies about 16,000 IU per MED of whole-body
exposure, which sits inside the 10,000-25,000 IU often quoted for 1 MED.

    IU ~ 16,000 x (fraction of skin exposed) x min(dose / MED, 1)

People differ in how much vitamin D a dose makes (age, skin, body size), so the
most possible is also given as a range: one MED over the whole body is often
quoted as making 10,000-25,000 IU (Holick). The time it takes to reach the
plateau doesn't depend on that, only on the UV and the burn threshold.

Expressing dose in MEDs roughly accounts for skin type, because melanin
slows vitamin D production and burning alike. Production levels off as
exposure continues (previtamin D reaches a photo-equilibrium), so the
estimate is capped at 1 MED. This is a coarse public-health approximation.
It uses the erythemal dose as a proxy for vitamin-D-effective UV, which
overstates production when the sun is low (early, late and winter), and it
ignores age, body fat and diet.
"""

from __future__ import annotations

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
)
DEFAULT_BODY_EXPOSURE = "face_arms"


def body_exposure(key: str) -> BodyExposure:
    for b in BODY_EXPOSURES:
        if b.key == key:
            return b
    raise KeyError(key)


def vitamin_d_iu(effective_sed: float, med_sed: float, body_fraction: float) -> float:
    """Estimated IU made by a dose (already reduced by any sunscreen)."""
    if med_sed <= 0 or body_fraction <= 0 or effective_sed <= 0:
        return 0.0
    meds = min(effective_sed / med_sed, PLATEAU_MED)
    return IU_PER_MED_WHOLE_BODY * body_fraction * meds


def dose_for_vitamin_d(target_iu: float, med_sed: float, body_fraction: float) -> float | None:
    """Effective dose (SED) needed to make ``target_iu``.

    Returns None when the target can't be reached below the plateau (i.e. it
    would need at least a full burn-threshold dose on that much skin).
    """
    if target_iu <= 0:
        return 0.0
    if med_sed <= 0 or body_fraction <= 0:
        return None
    meds = target_iu / (IU_PER_MED_WHOLE_BODY * body_fraction)
    if meds >= PLATEAU_MED:
        return None
    return meds * med_sed


def most_before_burning(body_fraction: float) -> float:
    """The most vitamin D (IU) a day's exposure can make before reaching the burn threshold."""
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
