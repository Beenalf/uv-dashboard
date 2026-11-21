from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from uv_dose.arpansa import FALLBACK_SITES, DayData, Sample, parse_day

DATA = Path(__file__).parent / "data"
MELBOURNE = next(s for s in FALLBACK_SITES if s.code == "mel")


def load_json(name: str):
    return json.loads((DATA / name).read_text(encoding="utf-8"))


@pytest.fixture
def mel_day() -> DayData:
    """Real ARPANSA data, Melbourne 2026-11-20 (a cloudy day; complete)."""
    return parse_day(load_json("uvlevel_mel_2026-11-20.json"), MELBOURNE, date(2026, 11, 20))


@pytest.fixture
def mel_partial_day() -> DayData:
    """Real ARPANSA data captured mid-afternoon: measurements stop at 15:17."""
    return parse_day(load_json("uvlevel_mel_2026-11-21_partial.json"), MELBOURNE, date(2026, 11, 21))


def constant_day(uvi: float, start: datetime, minutes: int) -> DayData:
    samples = [Sample(start + timedelta(minutes=i), uvi, uvi) for i in range(minutes)]
    return DayData(site=MELBOURNE, day=start.date(), samples=samples)
