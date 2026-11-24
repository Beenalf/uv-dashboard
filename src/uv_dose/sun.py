"""Where the sun is: how far from overhead, for a site and its local clock time.

Uses NOAA's low-accuracy solar position equations (declination and equation of time
from the day of the year). They are good to a fraction of a degree, far better
than the vitamin D conversion that uses them needs.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from typing import Sequence

from uv_dose.arpansa import Site, observes_dst


def _declination_and_equation_of_time(day: date) -> tuple[float, float]:
    """The sun's declination (radians) and the equation of time (minutes) at noon on ``day``."""
    days_in_year = 366 if day.year % 4 == 0 and (day.year % 100 != 0 or day.year % 400 == 0) else 365
    g = 2 * math.pi / days_in_year * (day.timetuple().tm_yday - 1)
    eot = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                    - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g)
            + 0.000907 * math.sin(2 * g) - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    return decl, eot


def utc_offset(site: Site, day: date) -> timedelta:
    """The site's clock offset from UTC on ``day``, including daylight saving."""
    return timedelta(minutes=site.utc_offset_minutes, hours=int(observes_dst(site, day)))


def cos_zenith(site: Site, day: date, times: Sequence[datetime]) -> list[float]:
    """The cosine of the sun's angle from overhead at each local clock time on ``day``.

    1 is straight overhead, 0 the horizon, negative below it. ARPANSA's times mark the
    start of each minute, so the middle of the minute is used.
    """
    decl, eot = _declination_and_equation_of_time(day)
    lat = math.radians(site.latitude)
    a, b = math.sin(lat) * math.sin(decl), math.cos(lat) * math.cos(decl)
    # Local clock minutes -> true solar time: back to UTC, then 4 minutes per degree of longitude.
    shift = -utc_offset(site, day).total_seconds() / 60 + 4 * site.longitude + eot + 0.5
    out = []
    for t in times:
        solar_minutes = t.hour * 60 + t.minute + t.second / 60 + shift
        hour_angle = math.radians(solar_minutes / 4 - 180)
        out.append(a + b * math.cos(hour_angle))
    return out


def solar_noon(site: Site, day: date) -> datetime:
    """Local clock time when the sun is highest."""
    _, eot = _declination_and_equation_of_time(day)
    minutes = 720 - 4 * site.longitude - eot + utc_offset(site, day).total_seconds() / 60
    return datetime.combine(day, datetime.min.time()) + timedelta(minutes=minutes)
