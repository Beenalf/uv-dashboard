"""Client for ARPANSA's UV data service.

ARPANSA documents a live XML feed (``/xml/uvvalues.xml``) that only carries the
*current* UV Index. The dose and UV-level charts on arpansa.gov.au are driven by
a JSON API on the same host, which returns one reading per minute for a whole
day (measured, plus ARPANSA's clear-sky forecast). Integrating a time window
needs the per-minute series, so this client uses that JSON API:

* ``api/categoriesSites``                           -> monitoring sites
* ``api/uvlevel/?longitude=..&latitude=..&date=..`` -> per-minute UV Index

The JSON API is not formally documented by ARPANSA and may change.

UV observations courtesy of ARPANSA.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from uv_dose import __version__

BASE_URL = "https://uvdata.arpansa.gov.au/"
USER_AGENT = f"uv-dose/{__version__} (desktop UV dose calculator; ARPANSA UV data)"
DISCLAIMER_URL = (
    "https://www.arpansa.gov.au/our-services/monitoring/ultraviolet-radiation-monitoring/"
    "ultraviolet-radiation-data-information-developers"
)
DOSE_PAGE_URL = (
    "https://www.arpansa.gov.au/our-services/monitoring/ultraviolet-radiation-monitoring/"
    "ultraviolet-radiation-dose"
)
# The per-minute API has nothing before 2016 (checked Nov 2026). Melbourne has the
# clear-sky forecast from early 2016 and measurements from late 2016; other sites vary.
EARLIEST_DATE = date(2016, 1, 1)

# Data for past days never changes, so it is cached for the session. Today's data
# gains a reading every minute, so it is re-fetched after this many seconds.
LIVE_CACHE_SECONDS = 60


class ArpansaError(RuntimeError):
    """Raised when ARPANSA data cannot be fetched or understood."""


@dataclass(frozen=True)
class Site:
    code: str
    name: str
    state: str
    latitude: float
    longitude: float
    utc_offset_minutes: int  # standard time; the API already returns local (DST-adjusted) times

    @property
    def label(self) -> str:
        return f"{self.name} ({self.state})"


@dataclass(frozen=True)
class Sample:
    """One minute of data. ``measured``/``forecast`` are UV Index values or None."""

    time: datetime
    measured: float | None
    forecast: float | None


@dataclass
class DayData:
    site: Site
    day: date
    samples: list[Sample]
    fetched_at: datetime = field(default_factory=datetime.now)
    outlook_from: date | None = None  # set on a future day built from this day's clear-sky forecast

    @property
    def has_measurements(self) -> bool:
        return any(s.measured is not None for s in self.samples)

    @property
    def has_forecast(self) -> bool:
        return any(s.forecast is not None for s in self.samples)

    @property
    def has_data(self) -> bool:
        return self.has_measurements or any(s.forecast for s in self.samples)

    @property
    def latest_measured(self) -> Sample | None:
        for s in reversed(self.samples):
            if s.measured is not None:
                return s
        return None

    @property
    def is_complete(self) -> bool:
        """True when the last minute of the day has a measurement (i.e. not a live day)."""
        return bool(self.samples) and self.samples[-1].measured is not None

    @property
    def first_time(self) -> datetime | None:
        return self.samples[0].time if self.samples else None

    @property
    def end_time(self) -> datetime | None:
        """End of the last sampled minute."""
        if not self.samples:
            return None
        return self.samples[-1].time + timedelta(minutes=1)


# Snapshot of api/categoriesSites (Nov 2026), used when the network is unavailable
# at start-up so the city list is never empty.
FALLBACK_SITES: tuple[Site, ...] = (
    Site("adl", "Adelaide", "SA", -34.95, 138.52, 570),
    Site("ali", "Alice Springs", "NT", -23.8, 133.89, 570),
    Site("bri", "Brisbane", "QLD", -27.45, 153.03, 600),
    Site("can", "Canberra", "ACT", -35.31, 149.2, 600),
    Site("dar", "Darwin", "NT", -12.43, 130.89, 570),
    Site("emd", "Emerald", "QLD", -23.5251, 148.161346, 600),
    Site("gco", "Gold Coast", "QLD", -28.0, 153.37, 600),
    Site("kin", "Kingston", "TAS", -42.99, 147.29, 600),
    Site("mel", "Melbourne", "VIC", -37.73, 145.1, 600),
    Site("new", "Newcastle", "NSW", -32.9, 151.72, 600),
    Site("per", "Perth", "WA", -31.93, 115.98, 480),
    Site("syd", "Sydney", "NSW", -34.04, 151.1, 600),
    Site("tow", "Townsville", "QLD", -19.33, 146.76, 600),
    Site("cas", "Casey", "Antarctica", -66.28, 110.53, 480),
    Site("dav", "Davis", "Antarctica", -68.58, 77.97, 420),
    Site("maw", "Mawson", "Antarctica", -67.6, 62.87, 300),
)
DEFAULT_SITE_CODE = "mel"


def _sort_key(site: Site) -> tuple[int, str]:
    # Australian cities first (alphabetical), Antarctic stations last.
    return (1 if site.state.lower() == "antarctica" else 0, site.name)


def sort_sites(sites: list[Site] | tuple[Site, ...]) -> list[Site]:
    return sorted(sites, key=_sort_key)


def parse_sites(payload: Any) -> list[Site]:
    if not isinstance(payload, list):
        raise ArpansaError("Unexpected site list format from ARPANSA")
    sites: list[Site] = []
    for row in payload:
        try:
            if not row.get("SiteEnabled", True) or not row.get("CategoryEnabled", True):
                continue
            sites.append(
                Site(
                    code=str(row["SiteIdentifier"]).strip(),
                    name=str(row["SiteName"]).strip(),
                    state=str(row["CategoryName"]).strip(),
                    latitude=float(row["SiteLatitude"]),
                    longitude=float(row["SiteLongitude"]),
                    utc_offset_minutes=int(row.get("SiteTimeZoneOffset") or 0),
                )
            )
        except (KeyError, TypeError, ValueError, AttributeError):
            continue  # skip malformed rows rather than failing the whole list
    if not sites:
        raise ArpansaError("ARPANSA returned no monitoring sites")
    return sort_sites(sites)


def _opt_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return max(v, 0.0)  # sensor noise can dip slightly below zero at night


# Older days (2016) carry the clear-sky forecast only every 10 minutes. Gaps up to
# this long are filled by linear interpolation (the forecast is a smooth curve).
FORECAST_FILL_LIMIT = timedelta(minutes=15)


def _fill_sparse_forecast(samples: list[Sample]) -> list[Sample]:
    """Interpolate the clear-sky forecast into minutes that lack it, so every minute counts.

    Without this, a 10-minute forecast would be integrated as if 9 of every 10 minutes
    had no UV, understating clear-sky doses about tenfold.
    """
    known = [i for i, s in enumerate(samples) if s.forecast is not None]
    if not known or len(known) == len(samples):
        return samples
    filled = [s.forecast for s in samples]
    for a, b in zip(known, known[1:]):
        ta, tb = samples[a].time, samples[b].time
        if b - a < 2 or tb - ta > FORECAST_FILL_LIMIT:
            continue
        fa, fb = samples[a].forecast, samples[b].forecast
        span = (tb - ta).total_seconds()
        for k in range(a + 1, b):
            frac = (samples[k].time - ta).total_seconds() / span
            filled[k] = fa + (fb - fa) * frac
    # Hold the first/last forecast value over the few minutes at the edges of the day.
    first, last = known[0], known[-1]
    for k in range(first):
        if samples[first].time - samples[k].time <= FORECAST_FILL_LIMIT:
            filled[k] = samples[first].forecast
    for k in range(last + 1, len(samples)):
        if samples[k].time - samples[last].time <= FORECAST_FILL_LIMIT:
            filled[k] = samples[last].forecast
    return [s if s.forecast == f else Sample(s.time, s.measured, f) for s, f in zip(samples, filled)]


def parse_day(payload: Any, site: Site, day: date) -> DayData:
    try:
        rows = payload["GraphData"]
    except (TypeError, KeyError) as exc:
        raise ArpansaError("Unexpected UV data format from ARPANSA") from exc
    samples: list[Sample] = []
    for row in rows or []:
        try:
            t = datetime.strptime(str(row["Date"]).strip(), "%Y-%m-%d %H:%M")
        except (KeyError, ValueError):
            continue
        samples.append(Sample(t, _opt_float(row.get("Measured")), _opt_float(row.get("Forecast"))))
    samples.sort(key=lambda s: s.time)
    return DayData(site=site, day=day, samples=_fill_sparse_forecast(samples))


# --------------------------------------------------------------------------- outlook for tomorrow

# ARPANSA publishes nothing for future dates (checked Nov 2026). A clear-sky
# forecast changes very little from one day to the next (the sun's path shifts
# by well under a degree), so tomorrow's clear-sky curve is taken from today's.
MAX_DAYS_AHEAD = 1

# States that use daylight saving: from 2 am on the first Sunday in October to
# 3 am on the first Sunday in April. The API's times are local clock times, so an
# outlook across a changeover must shift by an hour.
DST_STATES = frozenset({"NSW", "VIC", "ACT", "TAS", "SA"})


def _first_sunday(year: int, month: int) -> date:
    d = date(year, month, 1)
    return d + timedelta(days=(6 - d.weekday()) % 7)


def observes_dst(site: Site, day: date) -> bool:
    """Whether the site's clocks are on daylight saving time during the day (from 6 am)."""
    if site.state.upper() not in DST_STATES:
        return False
    return day >= _first_sunday(day.year, 10) or day < _first_sunday(day.year, 4)


def clear_sky_outlook(base: DayData, target: date) -> DayData:
    """A clear-sky stand-in for ``target`` (a future day), from ``base``'s forecast.

    Measurements are dropped. Times move to ``target``, plus or minus an hour if
    daylight saving starts or ends in between, so the curve stays on the sun's
    schedule.
    """
    shift = target - base.day
    shift += timedelta(hours=int(observes_dst(base.site, target)) - int(observes_dst(base.site, base.day)))
    samples = [Sample(s.time + shift, None, s.forecast) for s in base.samples]
    return DayData(site=base.site, day=target, samples=samples, fetched_at=base.fetched_at, outlook_from=base.day)


class ArpansaClient:
    """Small, thread-safe JSON client with an in-memory cache."""

    def __init__(self, base_url: str = BASE_URL, timeout: float = 20.0) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.timeout = timeout
        self._cache: dict[tuple[str, date], tuple[float, DayData]] = {}
        self._lock = threading.Lock()
        self._fetching: dict[tuple[str, date], threading.Lock] = {}  # one download per site and day at a time

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise ArpansaError(f"ARPANSA returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise ArpansaError(f"Could not reach ARPANSA ({reason})") from exc
        try:
            return json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ArpansaError("ARPANSA sent data that could not be read") from exc

    def sites(self) -> list[Site]:
        return parse_sites(self._get_json("api/categoriesSites"))

    def outlook(self, site: Site, day: date, *, force: bool = False) -> DayData:
        """Clear-sky outlook for a future ``day``, from today's forecast."""
        return clear_sky_outlook(self.day(site, date.today(), force=force), day)

    def day(self, site: Site, day: date, *, force: bool = False) -> DayData:
        key = (site.code, day)
        with self._lock:
            fetching = self._fetching.setdefault(key, threading.Lock())
        # A caller that arrives while the same day is downloading (e.g. the window asking for
        # the day that ``prefetch`` started on) waits for that download and uses it.
        with fetching:
            now = time.monotonic()
            with self._lock:
                cached = self._cache.get(key)
            if cached and not force:
                fetched, data = cached
                if data.is_complete or now - fetched < LIVE_CACHE_SECONDS:
                    return data
            payload = self._get_json(
                "api/uvlevel/",
                {"longitude": site.longitude, "latitude": site.latitude, "date": day.isoformat()},
            )
            data = parse_day(payload, site, day)
            with self._lock:
                self._cache[key] = (now, data)
            return data

    def prefetch(self, site: Site, day: date) -> None:
        """Start downloading a day in the background, so a later ``day()`` call finds it ready."""
        def fetch() -> None:
            try:
                self.day(site, day)
            except ArpansaError:
                pass  # the window's own request reports the problem

        threading.Thread(target=fetch, daemon=True).start()
