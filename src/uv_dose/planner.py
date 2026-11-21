"""Sun-time planner: from a start time, how long until the vitamin D target and the burn threshold."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Sequence

from uv_dose.dose import UVPoint, dose_until_end, time_to_dose
from uv_dose.vitamin_d import dose_for_vitamin_d


@dataclass(frozen=True)
class SunPlan:
    start: datetime
    med_sed: float
    spf: float
    target_iu: float
    body_fraction: float
    burn_at: datetime | None  # None: burn threshold not reached before the day's data ends
    vitd_dose_sed: float | None  # None: target unreachable below the burn threshold
    vitd_at: datetime | None  # None: not reachable today (or unreachable at all)
    dose_available: float  # effective dose from start to end of the day's data
    relies_on_forecast: bool  # part of the computed span comes from the clear-sky forecast

    @property
    def burn_after(self) -> timedelta | None:
        return self.burn_at - self.start if self.burn_at else None

    @property
    def vitd_after(self) -> timedelta | None:
        return self.vitd_at - self.start if self.vitd_at else None

    @property
    def vitd_reachable_today(self) -> bool:
        return self.vitd_at is not None


def plan_exposure(
    series: Sequence[UVPoint],
    start: datetime,
    *,
    med_sed: float,
    spf: float = 1.0,
    target_iu: float = 1000,
    body_fraction: float = 0.25,
) -> SunPlan:
    spf = max(spf, 1.0)
    burn_at = time_to_dose(series, start, med_sed, spf)
    vitd_dose = dose_for_vitamin_d(target_iu, med_sed, body_fraction)
    vitd_at = time_to_dose(series, start, vitd_dose, spf) if vitd_dose is not None else None

    horizon = burn_at or (series[-1].time + timedelta(minutes=1) if series else start)
    relies = any(p.kind != "measured" for p in series if start <= p.time < horizon)

    return SunPlan(
        start=start,
        med_sed=med_sed,
        spf=spf,
        target_iu=target_iu,
        body_fraction=body_fraction,
        burn_at=burn_at,
        vitd_dose_sed=vitd_dose,
        vitd_at=vitd_at,
        dose_available=dose_until_end(series, start, spf),
        relies_on_forecast=relies,
    )
