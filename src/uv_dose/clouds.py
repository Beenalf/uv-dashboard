"""Cloud outlook: how much of the clear-sky UV is likely to get through later today.

ARPANSA publishes measurements up to now and a clear-sky forecast for the whole day.
Used alone, the forecast assumes a cloudless sky for every minute ahead, which on a
cloudy day can overstate the UV still to come two- or threefold.

The likely UV for a minute ahead is ARPANSA's clear-sky forecast times a cloud factor:
the share of clear-sky UV expected to get through. Two estimates are blended:

* recent: the last hour's measured UV over its clear-sky forecast. Cloud changes
  slowly, so this is the better guess for the next hour or so.
* forecast: Open-Meteo's hourly UV forecast with cloud over its forecast without
  (from the GFS weather model). This is better from a couple of hours ahead.

The weight on the recent factor falls as exp(-minutes ahead / 120). Without a
forecast (e.g. Open-Meteo can't be reached), the recent factor fades towards clear
sky instead, over about 3 hours, so the fallback errs on the side of more UV. Before
the first reading of the day there is no recent factor, so the forecast is used
alone. Factors are capped at 1: brief boosts above clear sky from broken cloud
can't be predicted.

Backtest (10 ARPANSA sites, 16-24 Nov 2026, five starting times a day; the dose
still to come compared with what ARPANSA then measured; typical = median error):

    cloudy cases (UV under 80% of clear sky)   next 2 h   rest of day   >25% too low
    clear-sky forecast alone                      74%         64%             0%
    last hour held                                24%         30%            26%
    Open-Meteo alone                              33%         26%            13%
    blend (used here)                             20%         18%             9%
    last hour fading to clear sky (fallback)      27%         34%             1%

Over all cases the blend is typically 9% off, against 20-22% for clear sky. Open-Meteo
keeps no old UV forecasts, so the backtest used its latest runs (0-6 hours old); a
day-old forecast of sunlight did almost as well, so the age matters little.
"""

from __future__ import annotations

import json
import math
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from uv_dose import __version__
from uv_dose.arpansa import DayData, Site
from uv_dose.sun import utc_offset

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_HOME = "https://open-meteo.com/"
USER_AGENT = f"uv-dose/{__version__} (desktop UV dose calculator)"

# GFS runs every 6 hours and Open-Meteo updates within a few hours of each, so the
# forecast is re-fetched after this long.
FORECAST_CACHE_SECONDS = 30 * 60

RECENT_MINUTES = 60  # the window for the recent factor
MIN_CLEAR_UVI = 0.5  # below this average clear-sky UV a ratio is mostly noise (dawn, dusk)
FADE_MINUTES = 120.0  # recent -> forecast
FALLBACK_FADE_MINUTES = 180.0  # recent -> clear sky, when there's no forecast


class CloudError(RuntimeError):
    """Raised when the cloud forecast cannot be fetched or understood."""


def _to_local(site: Site, utc: datetime) -> datetime:
    """A UTC time as the site's local clock time (the clock ARPANSA's data uses)."""
    standard = utc + timedelta(minutes=site.utc_offset_minutes)
    return utc + utc_offset(site, standard.date())


@dataclass(frozen=True)
class CloudForecast:
    """Open-Meteo's cloud factor for a site, by hour: (local time at the middle of the hour, factor).

    Only hours with enough clear-sky UV for a meaningful ratio are kept, so the
    factor is held flat from the first such hour back to dawn, and from the last
    on to dusk.
    """

    site_code: str
    hours: tuple[tuple[datetime, float], ...]  # sorted by time
    _times: list[datetime] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_times", [t for t, _ in self.hours])

    def covers(self, day: date) -> bool:
        """Whether the forecast has any daylight hours on ``day`` (a date)."""
        return any(t.date() == day for t in self._times)

    def factor_at(self, t: datetime) -> float | None:
        """The factor at local time ``t``, interpolated between hours of the same day; None if that day isn't covered."""
        i = bisect_left(self._times, t)
        before = self.hours[i - 1] if i > 0 and self._times[i - 1].date() == t.date() else None
        after = self.hours[i] if i < len(self.hours) and self._times[i].date() == t.date() else None
        if before and after:
            span = (after[0] - before[0]).total_seconds()
            frac = (t - before[0]).total_seconds() / span if span else 1.0
            return before[1] + (after[1] - before[1]) * frac
        if before or after:
            return (before or after)[1]
        return None


def parse_forecast(payload: Any, site: Site) -> CloudForecast:
    """Read Open-Meteo's hourly ``uv_index`` and ``uv_index_clear_sky`` (times in GMT).

    Each value is the average over the hour before its time stamp (a clear-sky day's
    values are symmetric about solar noon only when read that way), so it's placed at
    the middle of that hour.
    """
    try:
        hourly = payload["hourly"]
        rows = zip(hourly["time"], hourly["uv_index"], hourly["uv_index_clear_sky"])
    except (TypeError, KeyError) as exc:
        raise CloudError("Unexpected cloud forecast format from Open-Meteo") from exc
    hours: list[tuple[datetime, float]] = []
    for stamp, uv, clear in rows:
        if uv is None or clear is None or clear < MIN_CLEAR_UVI:
            continue
        try:
            utc = datetime.fromisoformat(str(stamp))
        except ValueError:
            continue
        hours.append((_to_local(site, utc - timedelta(minutes=30)), max(0.0, min(float(uv) / float(clear), 1.0))))
    if not hours:
        raise CloudError("Open-Meteo sent no UV forecast for this place")
    hours.sort()
    return CloudForecast(site.code, tuple(hours))


class OpenMeteoClient:
    """Fetches Open-Meteo's hourly UV forecast for a site, cached for half an hour. Thread-safe."""

    def __init__(self, url: str = OPEN_METEO_URL, timeout: float = 20.0) -> None:
        self.url = url
        self.timeout = timeout
        self._cache: dict[str, tuple[float, CloudForecast]] = {}
        self._lock = threading.Lock()
        self._fetching: dict[str, threading.Lock] = {}

    def forecast(self, site: Site) -> CloudForecast:
        with self._lock:
            fetching = self._fetching.setdefault(site.code, threading.Lock())
        with fetching:  # one download per site at a time; a second caller uses the first's result
            now = time.monotonic()
            with self._lock:
                cached = self._cache.get(site.code)
            if cached and now - cached[0] < FORECAST_CACHE_SECONDS:
                return cached[1]
            # GMT times, converted with the site's own offsets; from yesterday to the day after
            # tomorrow, so today and tomorrow are whole in any Australian time zone.
            params = {"latitude": site.latitude, "longitude": site.longitude,
                      "hourly": "uv_index,uv_index_clear_sky", "timezone": "GMT", "past_days": 1, "forecast_days": 3}
            forecast = parse_forecast(self._get_json(params), site)
            with self._lock:
                self._cache[site.code] = (now, forecast)
            return forecast

    def _get_json(self, params: dict[str, Any]) -> Any:
        url = self.url + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise CloudError(f"Open-Meteo returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise CloudError(f"Could not reach Open-Meteo ({getattr(exc, 'reason', exc)})") from exc
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CloudError("Open-Meteo sent data that could not be read") from exc


# --------------------------------------------------------------------------- the outlook


def recent_factor(day: DayData) -> float | None:
    """The share of clear-sky UV measured over the hour up to the latest reading (None if too little to go on)."""
    latest = day.latest_measured
    if latest is None:
        return None
    since = latest.time - timedelta(minutes=RECENT_MINUTES - 1)
    pairs = [(s.measured, s.forecast) for s in day.samples
             if since <= s.time <= latest.time and s.measured is not None and s.forecast is not None]
    clear = sum(f for _, f in pairs)
    if len(pairs) < RECENT_MINUTES // 2 or clear < MIN_CLEAR_UVI * len(pairs):
        return None
    return min(sum(m for m, _ in pairs) / clear, 1.0)


@dataclass(frozen=True)
class CloudOutlook:
    """The likely share of clear-sky UV for each minute from ``start`` on (see the module notes)."""

    start: datetime  # the first minute without a reading: after the latest one, or the day's first minute
    recent: float | None
    forecast: CloudForecast | None

    def factor(self, t: datetime) -> float:
        ahead = self.forecast.factor_at(t) if self.forecast else None
        if self.recent is None:
            return 1.0 if ahead is None else ahead
        lead = max((t - self.start).total_seconds() / 60, 0.0)
        if ahead is None:
            w = math.exp(-lead / FALLBACK_FADE_MINUTES)
            return w * self.recent + (1 - w)
        w = math.exp(-lead / FADE_MINUTES)
        return w * self.recent + (1 - w) * ahead


def cloud_outlook(day: DayData, forecast: CloudForecast | None) -> CloudOutlook | None:
    """The outlook for the minutes of ``day`` after its latest reading.

    None when there's nothing to adjust: the day is complete, has no clear-sky
    forecast, or has neither a recent factor nor a cloud forecast for its date.
    """
    if day.is_complete or not day.has_forecast or not day.samples:
        return None
    if forecast is not None and (forecast.site_code != day.site.code or not forecast.covers(day.day)):
        forecast = None
    latest = day.latest_measured
    recent = recent_factor(day)
    if recent is None and forecast is None:
        return None
    start = latest.time + timedelta(minutes=1) if latest else day.samples[0].time
    return CloudOutlook(start, recent, forecast)
