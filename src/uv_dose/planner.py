"""Planning a day in the sun: one or more outings, each with its own conditions.

Sunburn and vitamin D both depend on the total dose your skin gets over the
day, so the outings' doses add up. Each outing has a ``factor``: the fraction of
ARPANSA's ambient dose that reaches the skin (shade, reflection, sunscreen; see
``uv_dose.exposure``).

The burn and vitamin D times follow the running dose through the outings in
order. If a threshold isn't reached by the end of the last outing, they carry on
as if you stayed out after it (in the same conditions) until the day's data ends.
That answers "how much longer could I stay out?". Times in the sun count only
the minutes spent outdoors.

Burning follows the sunburn-weighted dose. Vitamin D follows the vitamin D dose:
the same dose with each minute weighted by its ``UVPoint.vitd_factor``, so a low
sun makes less vitamin D for the same sunburn risk.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Sequence

from uv_dose.dose import ONE_MINUTE, UVPoint, WindowDose, hourly_breakdown, in_window, minute_dose, window_dose
from uv_dose.vitamin_d import PLATEAU_MED, dose_for_vitamin_d, vitamin_d_iu


@dataclass(frozen=True)
class Outing:
    start: datetime
    end: datetime
    factor: float = 1.0  # fraction of the ambient dose that reaches the skin

    @property
    def duration(self) -> timedelta:
        return max(self.end - self.start, timedelta(0))


def trim_overlaps(outings: Sequence[Outing]) -> tuple[list[Outing], bool]:
    """Sort outings by start and trim overlaps, so no minute counts twice.

    An outing that starts inside an earlier one is moved to start where the
    earlier one ends (it can shrink to nothing). Returns the outings and
    whether anything was trimmed.
    """
    result: list[Outing] = []
    reach: datetime | None = None
    trimmed = False
    for o in sorted(outings, key=lambda o: (o.start, o.end)):
        start = o.start
        if reach is not None and start < reach:
            start = min(reach, o.end)
            trimmed = True
        result.append(Outing(start, max(o.end, start), o.factor))
        reach = o.end if reach is None else max(reach, o.end)
    return result, trimmed


@dataclass(frozen=True)
class Crossing:
    """The moment the running skin dose reaches a target."""

    at: datetime
    sun_time: timedelta  # time spent in the sun until then (outings, then staying out after the last)
    outing: int | None  # index of the outing it happens in; None if only by staying out after the last


@dataclass(frozen=True)
class _Segment:
    start: datetime
    end: datetime
    factor: float
    outing: int | None


def _segments(series: Sequence[UVPoint], outings: Sequence[Outing]) -> list[_Segment]:
    """The outings, then "staying out longer" after the last one until the day's data ends."""
    segs = [_Segment(o.start, o.end, o.factor, i) for i, o in enumerate(outings) if o.end > o.start]
    if segs and series:
        data_end = series[-1].time + ONE_MINUTE
        last = segs[-1]
        if last.end < data_end:
            segs.append(_Segment(last.end, data_end, last.factor, None))
    return segs


def _dose(p: UVPoint, factor: float, vitd: bool) -> float:
    """One minute's dose on the skin: sunburn-weighted, or vitamin D-weighted if ``vitd``."""
    d = minute_dose(p.uvi) * factor
    return d * p.vitd_factor if vitd else d


def _crossing(series: Sequence[UVPoint], segs: Sequence[_Segment], target: float,
              vitd: bool = False) -> Crossing | None:
    if not segs:
        return None
    if target <= 0:
        return Crossing(segs[0].start, timedelta(0), segs[0].outing)
    total = 0.0
    elapsed = timedelta(0)
    for seg in segs:
        for p in in_window(series, seg.start, seg.end):
            d = _dose(p, seg.factor, vitd)
            if d > 0 and total + d >= target:
                at = p.time + timedelta(minutes=(target - total) / d)
                return Crossing(at, elapsed + (at - seg.start), seg.outing)
            total += d
        elapsed += seg.end - seg.start
    return None


def _dose_until(series: Sequence[UVPoint], segs: Sequence[_Segment], until: datetime, vitd: bool) -> float:
    """The running dose through the segments up to ``until`` (a partial minute counts in proportion)."""
    total = 0.0
    for seg in segs:
        for p in in_window(series, seg.start, seg.end):
            if p.time >= until:
                return total
            total += _dose(p, seg.factor, vitd) * min((until - p.time) / ONE_MINUTE, 1.0)
    return total


@dataclass(frozen=True)
class VitaminDLimit:
    """What caps the day's vitamin D, when, and the vitamin D dose by then.

    "levels off": production reaches its plateau before you'd burn.
    "burn": you'd start to burn first (a low sun makes little vitamin D per unit of sunburn UV).
    "day": neither happens before the day's data ends, even staying out.
    """

    kind: Literal["levels off", "burn", "day"]
    at: datetime | None
    vitd_sed: float


@dataclass(frozen=True)
class DayPlan:
    outings: tuple[Outing, ...]  # sorted, overlaps trimmed
    overlapped: bool  # some outings overlapped and were trimmed
    windows: tuple[WindowDose, ...]  # ambient dose per outing
    med_sed: float
    target_iu: float
    body_fraction: float
    burn: Crossing | None  # None: not reached even staying out until the day's data ends
    vitd_dose_sed: float | None  # vitamin D dose the goal needs; None: more than the skin can make in a day
    vitd: Crossing | None
    dose_available: float  # skin dose over the outings plus staying out after the last one
    relies_on_forecast: bool  # part of the span up to the burn time comes from the clear-sky forecast
    vitd_doses: tuple[float, ...] = ()  # vitamin D dose on the skin per outing
    limit: VitaminDLimit | None = None

    @property
    def skin_doses(self) -> tuple[float, ...]:
        return tuple(w.sed * o.factor for w, o in zip(self.windows, self.outings))

    @property
    def ambient_sed(self) -> float:
        return sum(w.sed for w in self.windows)

    @property
    def skin_sed(self) -> float:
        return sum(self.skin_doses)

    @property
    def sun_time(self) -> timedelta:
        return sum((o.duration for o in self.outings), timedelta(0))

    @property
    def minutes(self) -> int:
        return sum(w.minutes for w in self.windows)

    @property
    def forecast_minutes(self) -> int:
        return sum(w.forecast_minutes for w in self.windows)

    @property
    def missing_minutes(self) -> int:
        return sum(w.missing_minutes for w in self.windows)

    @property
    def peak(self) -> WindowDose | None:
        """The outing window with the highest UV."""
        return max((w for w in self.windows if w.peak_time), key=lambda w: w.peak_uvi, default=None)

    @property
    def mean_uvi(self) -> float:
        n = self.minutes
        return sum(w.mean_uvi * w.minutes for w in self.windows) / n if n else 0.0

    @property
    def vitd_iu(self) -> float:
        """Estimated vitamin D from the outings themselves."""
        return vitamin_d_iu(sum(self.vitd_doses), self.med_sed, self.body_fraction)

    @property
    def most_iu(self) -> float:
        """The most vitamin D the day allows with these outings: see ``limit``."""
        return vitamin_d_iu(self.limit.vitd_sed, self.med_sed, self.body_fraction) if self.limit else 0.0

    @property
    def vitd_after_burn(self) -> bool:
        """The goal is only reached after you'd start to burn."""
        return self.vitd is not None and self.burn is not None and self.vitd.at > self.burn.at


def plan_day(
    series: Sequence[UVPoint],
    outings: Sequence[Outing],
    *,
    med_sed: float,
    target_iu: float = 1000,
    body_fraction: float = 0.25,
) -> DayPlan:
    trimmed, overlapped = trim_overlaps(outings)
    segs = _segments(series, trimmed)
    burn = _crossing(series, segs, med_sed)
    vitd_dose = dose_for_vitamin_d(target_iu, med_sed, body_fraction)
    vitd = _crossing(series, segs, vitd_dose, vitd=True) if vitd_dose is not None else None
    plateau = _crossing(series, segs, med_sed * PLATEAU_MED, vitd=True)
    if plateau and (burn is None or plateau.at <= burn.at):
        limit = VitaminDLimit("levels off", plateau.at, med_sed * PLATEAU_MED)
    elif burn:
        limit = VitaminDLimit("burn", burn.at, _dose_until(series, segs, burn.at, vitd=True))
    else:
        limit = VitaminDLimit("day", None, sum(_dose(p, seg.factor, True) for seg in segs
                                               for p in in_window(series, seg.start, seg.end)))

    available = 0.0
    relies = False
    for seg in segs:
        for p in in_window(series, seg.start, seg.end):
            available += minute_dose(p.uvi) * seg.factor
            if p.kind != "measured" and (burn is None or p.time < burn.at):
                relies = True

    return DayPlan(
        outings=tuple(trimmed),
        overlapped=overlapped,
        windows=tuple(window_dose(series, o.start, o.end) for o in trimmed),
        med_sed=med_sed,
        target_iu=target_iu,
        body_fraction=body_fraction,
        burn=burn,
        vitd_dose_sed=vitd_dose,
        vitd=vitd,
        dose_available=available,
        relies_on_forecast=relies,
        vitd_doses=tuple(sum(_dose(p, o.factor, True) for p in in_window(series, o.start, o.end))
                         for o in trimmed),
        limit=limit,
    )


# --------------------------------------------------------------------------- chart curves

Curve = tuple[list[datetime], list[float]]


def plan_curves(series: Sequence[UVPoint], outings: Sequence[Outing]) -> tuple[Curve, Curve]:
    """Running skin dose across the outings, and its continuation if you stayed out after the last.

    The first curve has a point at each outing's start and at the end of every
    minute in it, so it rises during outings and stays flat between them.
    """
    times: list[datetime] = []
    values: list[float] = []
    total = 0.0
    for o in outings:
        if o.end <= o.start:
            continue
        times.append(o.start)
        values.append(total)
        for p in in_window(series, o.start, o.end):
            total += minute_dose(p.uvi) * o.factor
            times.append(p.time + ONE_MINUTE)
            values.append(total)
    tail: Curve = ([], [])
    segs = _segments(series, outings)
    if segs and segs[-1].outing is None:
        seg = segs[-1]
        tail = ([seg.start], [total])
        running = total
        for p in in_window(series, seg.start, seg.end):
            running += minute_dose(p.uvi) * seg.factor
            tail[0].append(p.time + ONE_MINUTE)
            tail[1].append(running)
    return (times, values), tail


def value_at(curve: Curve, t: datetime) -> float | None:
    """The curve's value at time ``t`` (held flat between points); None before it starts."""
    times, values = curve
    i = bisect_right(times, t) - 1
    return values[i] if i >= 0 else None


# --------------------------------------------------------------------------- hourly table


@dataclass(frozen=True)
class PlanHour:
    outing: int
    start: datetime
    end: datetime
    minutes: int
    mean_uvi: float
    peak_uvi: float
    sed: float  # ambient dose in this row
    cumulative_sed: float  # ambient, running across all outings
    skin_cumulative_sed: float  # on the skin, running across all outings
    forecast_minutes: int
    missing_minutes: int


def plan_hours(series: Sequence[UVPoint], outings: Sequence[Outing]) -> list[PlanHour]:
    """Dose per clock hour within each outing, with running totals across the whole plan."""
    rows: list[PlanHour] = []
    ambient = skin = 0.0
    for i, o in enumerate(outings):
        if o.end <= o.start:
            continue
        for r in hourly_breakdown(series, o.start, o.end):
            ambient += r.sed
            skin += r.sed * o.factor
            rows.append(PlanHour(i, r.start, r.end, r.minutes, r.mean_uvi, r.peak_uvi, r.sed, ambient, skin,
                                 r.forecast_minutes, r.missing_minutes))
    return rows
