from __future__ import annotations

import threading
import time
from datetime import date, timedelta

import pytest

from conftest import MELBOURNE, load_json
from uv_dose.arpansa import ArpansaClient, ArpansaError, parse_day, parse_sites


def test_parse_sites_filters_and_sorts():
    rows = [
        {"SiteIdentifier": "maw", "SiteName": "Mawson   ", "CategoryName": "Antarctica ", "SiteLatitude": -67.6,
         "SiteLongitude": 62.87, "SiteEnabled": True, "CategoryEnabled": True, "SiteTimeZoneOffset": 300},
        {"SiteIdentifier": "syd", "SiteName": "Sydney  ", "CategoryName": "NSW   ", "SiteLatitude": -34.04,
         "SiteLongitude": 151.1, "SiteEnabled": True, "CategoryEnabled": True, "SiteTimeZoneOffset": 600},
        {"SiteIdentifier": "old", "SiteName": "Old", "CategoryName": "X", "SiteLatitude": 0,
         "SiteLongitude": 0, "SiteEnabled": False, "CategoryEnabled": True},
        {"broken": True},
    ]
    sites = parse_sites(rows)
    assert [s.code for s in sites] == ["syd", "maw"]
    assert sites[0].label == "Sydney (NSW)"


def test_parse_sites_rejects_garbage():
    with pytest.raises(ArpansaError):
        parse_sites({"not": "a list"})
    with pytest.raises(ArpansaError):
        parse_sites([])


def test_parse_day_handles_nulls_and_negatives():
    payload = {"GraphData": [
        {"Date": "2026-11-20 06:01", "Forecast": 0.1, "Measured": -0.002},
        {"Date": "2026-11-20 06:00", "Forecast": None, "Measured": None},
        {"Date": "garbage", "Forecast": 1, "Measured": 1},
    ]}
    day = parse_day(payload, MELBOURNE, date(2026, 11, 20))
    assert [s.time.minute for s in day.samples] == [0, 1]  # sorted, bad row dropped
    assert day.samples[0].measured is None and day.samples[1].measured == 0.0


def test_parse_day_fills_ten_minute_forecast():
    # 2016 days carry the forecast every 10 minutes (at :05, :15, ...); every minute must count.
    known = {5: 1.0, 15: 2.0, 25: 3.0}
    payload = {"GraphData": [
        {"Date": f"2016-06-01 06:{m:02d}", "Forecast": known.get(m), "Measured": None} for m in range(30)
    ]}
    day = parse_day(payload, MELBOURNE, date(2016, 6, 1))
    fc = [s.forecast for s in day.samples]
    assert fc[5] == 1.0 and fc[10] == pytest.approx(1.5) and fc[6] == pytest.approx(1.1)
    assert fc[:5] == [1.0] * 5 and fc[26:] == [3.0] * 4  # edges hold the nearest value
    assert all(s.measured is None for s in day.samples)


def test_parse_day_leaves_long_forecast_gaps():
    payload = {"GraphData": [
        {"Date": f"2016-06-01 {6 + m // 60:02d}:{m % 60:02d}", "Forecast": 1.0 if m in (0, 59) else None,
         "Measured": None} for m in range(60)
    ]}
    fc = [s.forecast for s in parse_day(payload, MELBOURNE, date(2016, 6, 1)).samples]
    assert fc[30] is None  # a 59-minute hole is an outage, not a sampling interval


def test_parse_day_rejects_garbage():
    with pytest.raises(ArpansaError):
        parse_day({"nope": []}, MELBOURNE, date(2026, 11, 20))


def test_complete_day_flags(mel_day):
    assert mel_day.has_data and mel_day.has_measurements and mel_day.is_complete
    assert len(mel_day.samples) == 840  # 06:00-19:59, one per minute


def test_prefetch_and_window_share_one_download():
    """The window's request for the day being prefetched waits for that download instead of repeating it."""
    calls = []

    class SlowClient(ArpansaClient):
        def _get_json(self, path, params=None):
            calls.append(params["date"])
            time.sleep(0.3)
            return load_json("uvlevel_mel_2026-11-20.json")

    client = SlowClient()
    day = date(2026, 11, 20)
    client.prefetch(MELBOURNE, day)
    time.sleep(0.05)  # the prefetch is now downloading
    results = []
    threads = [threading.Thread(target=lambda: results.append(client.day(MELBOURNE, day))) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert calls == ["2026-11-20"]
    assert len(results) == 2 and all(r.is_complete for r in results)
    client.day(MELBOURNE, day, force=True)  # Refresh still downloads again
    assert len(calls) == 2


@pytest.mark.network
def test_live_api_smoke():
    client = ArpansaClient(timeout=30)
    sites = client.sites()
    mel = next(s for s in sites if s.code == "mel")
    day = client.day(mel, date.today() - timedelta(days=1))
    assert day.has_data and len(day.samples) > 600
