"""UV dose arithmetic: per-minute UV Index -> Standard Erythemal Dose (SED).

Definitions
-----------
* UV Index (UVI) = erythemally weighted irradiance (W/m^2) x 40,
  so 1 UVI = 0.025 W/m^2 (25 mW/m^2).
* 1 SED = 100 J/m^2 of erythemally weighted UV.
* One minute at UVI ``u`` therefore delivers ``u * 0.025 * 60 / 100 = 0.015 * u`` SED.

ARPANSA's UV-level data has one reading per minute, so a dose is the sum of
``0.015 * uvi`` over the minutes in the window. Summing a full day this way
reproduces ARPANSA's own dose chart (e.g. Melbourne 2026-11-20: 16.6 SED
measured / 54.0 SED clear-sky, vs 17 / 54 on arpansa.gov.au, which rounds to
whole SEDs).

The dose is "ambient": what a horizontal, unshaded surface receives. Skin
facing other directions, shade and clothing all reduce it.

Minutes without a reading (later today, or a day ahead) use ARPANSA's clear-sky
forecast, scaled for cloud when there's a cloud outlook (``uv_dose.clouds``): those
are the "likely" minutes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, Sequence

from uv_dose.arpansa import DayData
from uv_dose.sun import cos_zenith
from uv_dose.vitamin_d import vitamin_d_factor

if TYPE_CHECKING:
    from uv_dose.clouds import CloudOutlook

UVI_WATTS_PER_M2 = 0.025
SED_JOULES_PER_M2 = 100.0
SED_PER_UVI_MINUTE = UVI_WATTS_PER_M2 * 60 / SED_JOULES_PER_M2  # 0.015

ONE_MINUTE = timedelta(minutes=1)

# "forecast": ARPANSA's clear-sky forecast as is. "likely": the clear-sky forecast scaled for cloud.
PointKind = Literal["measured", "forecast", "likely", "missing"]


class Source(StrEnum):
    """Which ARPANSA series to integrate."""

    BEST = "best"  # measured where available; elsewhere the forecast, scaled for cloud if there's an outlook
    CLEAR_SKY = "clear_sky"  # ARPANSA's clear-sky forecast only (a cloudless upper bound)


@dataclass(frozen=True)
class UVPoint:
    time: datetime  # start of the minute
    uvi: float
    kind: PointKind
    vitd_factor: float = 1.0  # vitamin D per unit of sunburn UV at this minute's sun angle (vitamin_d_factor)


def build_series(day: DayData, source: Source = Source.BEST, clouds: CloudOutlook | None = None) -> list[UVPoint]:
    """One point per minute of ``day``.

    With ``clouds`` (and ``Source.BEST``), minutes from ``clouds.start`` on that have
    no reading get the clear-sky forecast times the outlook's cloud factor.
    """
    factors = [vitamin_d_factor(mu) for mu in cos_zenith(day.site, day.day, [s.time for s in day.samples])]
    scale = clouds if source is Source.BEST else None
    points: list[UVPoint] = []
    for s, f in zip(day.samples, factors):
        if source is Source.BEST and s.measured is not None:
            points.append(UVPoint(s.time, s.measured, "measured", f))
        elif s.forecast is not None and scale is not None and s.time >= scale.start:
            points.append(UVPoint(s.time, s.forecast * scale.factor(s.time), "likely", f))
        elif s.forecast is not None:
            points.append(UVPoint(s.time, s.forecast, "forecast", f))
        else:
            points.append(UVPoint(s.time, 0.0, "missing", f))
    return points


def minute_dose(uvi: float, spf: float = 1.0) -> float:
    return uvi * SED_PER_UVI_MINUTE / max(spf, 1.0)


def in_window(series: Sequence[UVPoint], start: datetime, end: datetime) -> list[UVPoint]:
    return [p for p in series if start <= p.time < end]


# --------------------------------------------------------------------------- window dose


@dataclass(frozen=True)
class WindowDose:
    start: datetime
    end: datetime
    minutes: int
    sed: float  # ambient (unprotected) dose
    peak_uvi: float
    peak_time: datetime | None
    mean_uvi: float
    forecast_minutes: int  # clear-sky forecast, as is
    missing_minutes: int
    likely_minutes: int = 0  # clear-sky forecast scaled for cloud

    @property
    def joules_per_m2(self) -> float:
        return self.sed * SED_JOULES_PER_M2

    @property
    def duration(self) -> timedelta:
        return self.end - self.start


def window_dose(series: Sequence[UVPoint], start: datetime, end: datetime) -> WindowDose:
    pts = in_window(series, start, end)
    sed = sum(minute_dose(p.uvi) for p in pts)
    peak = max(pts, key=lambda p: p.uvi, default=None)
    return WindowDose(
        start=start,
        end=end,
        minutes=len(pts),
        sed=sed,
        peak_uvi=peak.uvi if peak else 0.0,
        peak_time=peak.time if peak else None,
        mean_uvi=(sum(p.uvi for p in pts) / len(pts)) if pts else 0.0,
        forecast_minutes=sum(1 for p in pts if p.kind == "forecast"),
        missing_minutes=sum(1 for p in pts if p.kind == "missing"),
        likely_minutes=sum(1 for p in pts if p.kind == "likely"),
    )


# --------------------------------------------------------------------------- hourly table


@dataclass(frozen=True)
class HourRow:
    start: datetime
    end: datetime
    minutes: int
    mean_uvi: float
    peak_uvi: float
    sed: float
    cumulative_sed: float
    forecast_minutes: int
    missing_minutes: int
    likely_minutes: int = 0


def hourly_breakdown(series: Sequence[UVPoint], start: datetime, end: datetime) -> list[HourRow]:
    """Dose per clock hour, clipped to the window (first/last rows may be partial hours)."""
    rows: list[HourRow] = []
    total = 0.0
    cursor = start
    while cursor < end:
        next_hour = cursor.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        stop = min(next_hour, end)
        pts = in_window(series, cursor, stop)
        if pts:
            sed = sum(minute_dose(p.uvi) for p in pts)
            total += sed
            rows.append(
                HourRow(
                    start=cursor,
                    end=stop,
                    minutes=len(pts),
                    mean_uvi=sum(p.uvi for p in pts) / len(pts),
                    peak_uvi=max(p.uvi for p in pts),
                    sed=sed,
                    cumulative_sed=total,
                    forecast_minutes=sum(1 for p in pts if p.kind == "forecast"),
                    missing_minutes=sum(1 for p in pts if p.kind == "missing"),
                    likely_minutes=sum(1 for p in pts if p.kind == "likely"),
                )
            )
        cursor = stop
    return rows


# --------------------------------------------------------------------------- ratings


@dataclass(frozen=True)
class UVCategory:
    name: str
    lower: float  # inclusive lower edge on the continuous UVI axis (rounding boundary)
    color: str  # WHO / ARPANSA UV Index colour
    advice: str


UV_CATEGORIES: tuple[UVCategory, ...] = (
    UVCategory("Low", 0.0, "#289500", "Sun protection generally not needed."),
    UVCategory("Moderate", 2.5, "#f7e400", "Sun protection recommended."),
    UVCategory("High", 5.5, "#f85900", "Sun protection recommended."),
    UVCategory("Very high", 7.5, "#d8001d", "Sun protection essential."),
    UVCategory("Extreme", 10.5, "#6b49c8", "Sun protection essential."),
)


def uv_category(uvi: float) -> UVCategory:
    """WHO category of a UV Index value (values are rounded to the nearest whole number first)."""
    rounded = math.floor(uvi + 0.5)
    for cat in reversed(UV_CATEGORIES):
        if rounded >= math.ceil(cat.lower):
            return cat
    return UV_CATEGORIES[0]


# Cancer Council and ARPANSA advise sun protection whenever the UV Index is 3 or
# above. Readings are rounded to whole numbers, so 2.5 counts as 3.
SUN_PROTECTION_UVI = 2.5


@dataclass(frozen=True)
class ProtectionTimes:
    start: datetime  # first minute at UV 3+
    end: datetime  # end of the last minute at UV 3+


def sun_protection_times(series: Sequence[UVPoint]) -> ProtectionTimes | None:
    """The span of the day when UV is 3 or above (SunSmart's "sun protection times")."""
    hits = [p.time for p in series if p.uvi >= SUN_PROTECTION_UVI]
    if not hits:
        return None
    return ProtectionTimes(hits[0], hits[-1] + ONE_MINUTE)


Level = Literal["low", "moderate", "high", "very_high"]


@dataclass(frozen=True)
class DoseRating:
    level: Level
    label: str
    advice: str
    fraction_of_med: float


def rate_dose(effective_sed: float, med_sed: float) -> DoseRating:
    """Rate a dose against a personal burn threshold (minimal erythemal dose, MED)."""
    frac = effective_sed / med_sed if med_sed > 0 else math.inf
    if frac < 0.5:
        return DoseRating("low", "Low", "Unlikely to burn.", frac)
    if frac < 1.0:
        return DoseRating("moderate", "Moderate", "Unlikely to burn, but UV damage still adds up.", frac)
    if frac < 2.0:
        return DoseRating("high", "High", "Sunburn likely.", frac)
    return DoseRating("very_high", "Very high", "Painful sunburn likely.", frac)
