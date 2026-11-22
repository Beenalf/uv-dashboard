from __future__ import annotations

from datetime import datetime

import matplotlib.dates as mdates

from uv_dose.gui.chart import ChartView, _hhmm


def test_time_labels_from_date_numbers():
    for h, m in ((6, 0), (12, 30), (19, 59), (0, 0)):
        assert _hhmm(mdates.date2num(datetime(2026, 11, 20, h, m))) == f"{h:02d}:{m:02d}"


def test_views_compare_by_content_not_hover_function():
    """The chart skips redrawing an identical view; a fresh hover function mustn't defeat that."""
    start, end = datetime(2026, 11, 20, 6), datetime(2026, 11, 20, 20)
    a = ChartView("Melbourne", start, end, times=[start], measured=[1.0], describe=lambda t: "a")
    b = ChartView("Melbourne", start, end, times=[start], measured=[1.0], describe=lambda t: "b")
    assert a == b
    assert a != ChartView("Melbourne", start, end, times=[start], measured=[2.0])
