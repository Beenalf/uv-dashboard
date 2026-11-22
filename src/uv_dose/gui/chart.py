"""The two-panel chart: UV Index over the day (top) and accumulated dose on skin (bottom).

Two panels share the time axis instead of one dual-axis plot, because UV Index
(an intensity) and SED (a dose) are different quantities on different scales.
Drag across either panel to choose a time window; click to set the start time.
"""

from __future__ import annotations

import math
import tkinter as tk
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

import matplotlib

matplotlib.use("TkAgg")

import matplotlib.dates as mdates  # noqa: E402
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402
from matplotlib.transforms import blended_transform_factory  # noqa: E402
from matplotlib.widgets import SpanSelector  # noqa: E402

from uv_dose.dose import UV_CATEGORIES  # noqa: E402
from uv_dose.gui.theme import Palette, mpl_font_family  # noqa: E402

DASH_FORECAST = (0, (4, 3))
DASH_THRESHOLD = (0, (6, 3))
DASH_TAIL = (0, (1.5, 2.5))
CLICK_TOLERANCE = timedelta(minutes=2)
CLICK_PIXELS = 4
PROTECTION_COLOR = UV_CATEGORIES[2].color  # the "High" orange: a caution colour that reads on light and dark


@dataclass
class ChartView:
    title: str
    day_start: datetime
    day_end: datetime
    times: list[datetime] = field(default_factory=list)
    measured: list[float | None] = field(default_factory=list)
    forecast: list[float | None] = field(default_factory=list)
    show_measured: bool = True
    latest_reading: datetime | None = None  # only on a live (today) chart
    outings: list[tuple[datetime, datetime]] = field(default_factory=list)
    selected: int | None = None  # index into ``outings`` of the one being edited
    protection: tuple[datetime, datetime] | None = None  # when UV is 3 or above
    curve: tuple[list[datetime], list[float]] = ((), ())  # dose on skin across the outings
    tail: tuple[list[datetime], list[float]] = ((), ())  # ... continued after the last outing
    clear_curve: tuple[list[datetime], list[float]] = ((), ())  # clear-sky equivalent, for comparison
    dose_label: str = "Dose on your skin"
    med_sed: float = 2.5
    vitd_sed: float | None = None
    burn_at: datetime | None = None
    vitd_at: datetime | None = None
    message: str | None = None  # shown instead of data (no data / loading / error)
    describe: Callable[[datetime], str] | None = None  # hover readout text
    hint: str = "Drag to pick a time window · click to set the start"


class DoseChart:
    def __init__(
        self,
        master: tk.Misc,
        palette: Palette,
        on_select: Callable[[datetime, datetime], None],
        on_click: Callable[[datetime], None],
    ) -> None:
        matplotlib.rcParams.update(
            {"font.family": mpl_font_family(), "font.size": 9, "axes.titlesize": 10, "svg.fonttype": "none"}
        )
        self.palette = palette
        self._on_select = on_select
        self._on_click = on_click
        self.fig = Figure(figsize=(8, 5.2), dpi=100, layout="constrained")
        self.ax_uv, self.ax_dose = self.fig.subplots(2, 1, sharex=True, height_ratios=[3, 2])
        self.canvas = FigureCanvasTkAgg(self.fig, master=master)
        self.widget = self.canvas.get_tk_widget()
        self.widget.configure(highlightthickness=0, borderwidth=0)
        self._selectors: list[SpanSelector] = []
        self._view: ChartView | None = None
        self._hover_lines: list = []
        self._readout = None
        self._hint = ""
        self._press: tuple[float, float] | None = None  # (pixel x, data x) of a left-button press
        self._threshold_labels: list = []  # (annotation, y in SED), bottom to top
        self.canvas.mpl_connect("draw_event", self._on_draw)
        self.canvas.mpl_connect("motion_notify_event", self._on_motion)
        self.canvas.mpl_connect("axes_leave_event", self._on_leave)
        self.canvas.mpl_connect("button_press_event", self._on_press)
        self.canvas.mpl_connect("button_release_event", self._on_release)

    # ---------------------------------------------------------------- public

    def set_palette(self, palette: Palette) -> None:
        self.palette = palette
        if self._view:
            self.render(self._view)

    def render(self, v: ChartView) -> None:
        self._view = v
        p = self.palette
        for sel in self._selectors:
            sel.disconnect_events()
        self._selectors.clear()

        ax1, ax2 = self.ax_uv, self.ax_dose
        ax1.cla()
        ax2.cla()
        self._threshold_labels = []
        self.fig.set_facecolor(p.surface)
        self.widget.configure(background=p.surface)
        for ax in (ax1, ax2):
            self._style_axes(ax)

        ax1.set_title(v.title, loc="left", color=p.ink, fontweight="bold", pad=10)
        ax1.set_xlim(v.day_start, v.day_end)
        self._format_time_axis(ax2)

        if v.message:
            ax1.set_ylim(0, 12)
            ax2.set_ylim(0, 3)
            ax1.text(0.5, 0.5, v.message, transform=ax1.transAxes, ha="center", va="center",
                     color=p.ink2, fontsize=11, wrap=True)
            ax1.set_ylabel("UV Index", color=p.ink2)
            ax2.set_ylabel("SED", color=p.ink2)
            self._readout = None
            self.canvas.draw_idle()
            return

        # ---------------- top panel: UV Index
        values = [x for x in (v.measured + v.forecast) if x is not None]
        ymax = max(12.0, math.ceil(max(values, default=0) + 1))
        ax1.set_ylim(0, ymax)
        band_tf = blended_transform_factory(ax1.transAxes, ax1.transData)
        for i, cat in enumerate(UV_CATEGORIES):
            lo = cat.lower
            hi = UV_CATEGORIES[i + 1].lower if i + 1 < len(UV_CATEGORIES) else ymax
            if lo >= ymax:
                break
            ax1.axhspan(lo, min(hi, ymax), color=cat.color, alpha=p.band_alpha, lw=0, zorder=0)
            ax1.text(1.008, (lo + min(hi, ymax)) / 2, cat.name, transform=band_tf, fontsize=8,
                     color=p.ink2, va="center", ha="left", clip_on=False)

        many = len(v.outings) > 1
        for i, (start, end) in enumerate(v.outings):
            chosen = i == v.selected or not many
            for ax in (ax1, ax2):
                ax.axvspan(start, end, color=p.series, alpha=p.select_alpha * (1 if chosen else 0.6), lw=0,
                           zorder=0.5)
                for edge in (start, end):
                    ax.axvline(edge, color=p.series, lw=0.8 if chosen else 0.6, alpha=0.6 if chosen else 0.3,
                               zorder=0.6)
        nan = float("nan")
        ax1.plot(v.times, [nan if x is None else x for x in v.forecast], color=p.forecast, lw=1.2,
                 ls=DASH_FORECAST, label="Clear-sky forecast", zorder=2)
        if v.show_measured:
            ax1.plot(v.times, [nan if x is None else x for x in v.measured], color=p.series, lw=1.6,
                     label="Measured", zorder=3, solid_capstyle="round")
        if v.latest_reading:
            ax1.axvline(v.latest_reading, color=p.muted, lw=0.9, zorder=1)
            ax1.annotate(f"Latest reading {v.latest_reading:%H:%M}", (v.latest_reading, ymax), xytext=(4, -4),
                         textcoords="offset points", va="top", ha="left", fontsize=8, color=p.ink2)
        if v.protection:
            self._draw_protection(*v.protection)
        ax1.set_ylabel("UV Index", color=p.ink2)
        ax1.legend(loc="upper left", frameon=False, fontsize=8, ncols=3, labelcolor=p.ink2,
                   bbox_to_anchor=(0, 1.0))

        # ---------------- bottom panel: dose on skin
        ct, cv = v.curve
        if ct:
            ax2.plot(ct, cv, color=p.series, lw=1.8, label=v.dose_label, zorder=3, solid_capstyle="round")
        tt, tv = v.tail
        if tt:
            ax2.plot(tt, tv, color=p.series, lw=1.3, alpha=0.55, ls=DASH_TAIL, label="If you stayed out longer",
                     zorder=2)
        kt, kv = v.clear_curve
        if kt:
            ax2.plot(kt, kv, color=p.forecast, lw=1.1, ls=DASH_FORECAST,
                     label="Same outings, clear sky" if len(v.outings) > 1 else "Same window, clear sky", zorder=2)

        top = max(v.med_sed * 1.3, (cv[-1] if cv else 0) * 1.15, (kv[-1] if kv else 0) * 1.05,
                  (v.vitd_sed or 0) * 1.3, 0.5)
        ax2.set_ylim(0, top)
        vitd_line = v.vitd_sed if v.vitd_sed is not None and v.vitd_sed < top else None
        ax2.axhline(v.med_sed, color=p.critical, lw=1.1, ls=DASH_THRESHOLD, zorder=1.5)
        if vitd_line is not None:
            ax2.axhline(vitd_line, color=p.good, lw=1.1, ls=DASH_THRESHOLD, zorder=1.5)
        self._label_thresholds(v.med_sed, vitd_line)

        # Event labels. The vitamin D time always comes before the burn time, so its label goes
        # left of its marker and the burn label above-left of its own (the curve is below there).
        # Labels near the left edge flip to the right side.
        span = (v.day_end - v.day_start) or timedelta(hours=1)
        for when, level, color, text, upper in (
            (v.vitd_at, v.vitd_sed, p.good, "Vitamin D", False),
            (v.burn_at, v.med_sed, p.critical, "Burn", True),
        ):
            if when is None or level is None:
                continue
            ax1.axvline(when, color=color, lw=1.0, alpha=0.9, zorder=2.5)
            ax2.axvline(when, color=color, lw=1.0, alpha=0.5, zorder=1.4)
            ax2.plot([when], [level], "o", ms=7, color=color, mec=p.surface, mew=2, zorder=5)
            near_left = (when - v.day_start) / span < 0.12
            if upper:
                xytext, ha, va = ((8, -5), "left", "top") if near_left else ((-6, 5), "right", "bottom")
            else:
                xytext, ha, va = ((8, 0), "left", "center") if near_left else ((-8, 0), "right", "center")
            ax2.annotate(f"{text} {when:%H:%M}", (when, level), xytext=xytext, textcoords="offset points",
                         va=va, ha=ha, fontsize=8, color=p.ink, fontweight="bold", zorder=6,
                         bbox={"boxstyle": "round,pad=0.25", "fc": p.surface, "ec": "none", "alpha": 0.85})

        ax2.set_ylabel("SED", color=p.ink2)
        if ct or tt or kt:
            ax2.legend(loc="upper left", frameon=False, fontsize=8, ncols=3, labelcolor=p.ink2,
                       bbox_to_anchor=(0, 1.02))

        # ---------------- interaction
        self._hint = v.hint
        self._readout = ax1.text(1.0, 1.02, self._hint, transform=ax1.transAxes, ha="right", va="bottom",
                                 fontsize=8, color=p.ink2)
        self._hover_lines = [
            ax.axvline(v.day_start, color=p.ink2, lw=0.8, alpha=0.7, visible=False, zorder=7) for ax in (ax1, ax2)
        ]
        for ax in (ax1, ax2):
            self._selectors.append(
                SpanSelector(ax, self._span_selected, "horizontal", useblit=True, minspan=0,
                             props={"facecolor": p.series, "alpha": 0.2}, interactive=False, button=1)
            )
        self.canvas.draw_idle()

    # ---------------------------------------------------------------- internals

    def _style_axes(self, ax) -> None:
        p = self.palette
        ax.set_facecolor(p.surface)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color(p.axis)
            ax.spines[side].set_linewidth(0.8)
        ax.tick_params(colors=p.ink2, labelcolor=p.ink2, length=3, width=0.6)
        ax.grid(axis="y", color=p.grid, lw=0.6, ls="-")
        ax.set_axisbelow(True)

    def _draw_protection(self, start: datetime, end: datetime) -> None:
        """A bar along the foot of the UV panel for when UV is 3 or above; the legend gives the times."""
        ax = self.ax_uv
        tf = blended_transform_factory(ax.transData, ax.transAxes)
        ax.plot([start, end], [0.018, 0.018], transform=tf, color=PROTECTION_COLOR, lw=5, solid_capstyle="butt",
                zorder=1.8, label=f"Sun protection {start:%H:%M}–{end:%H:%M} (UV 3+)")

    def _label_thresholds(self, med: float, vitd: float | None) -> None:
        """Name the threshold lines in the right margin (like the UV bands above)."""
        tf = blended_transform_factory(self.ax_dose.transAxes, self.ax_dose.transData)
        self._threshold_labels = [
            (self.ax_dose.annotate(text, (1, y), xycoords=tf, xytext=(5, 0), textcoords="offset points",
                                   ha="left", va="center", fontsize=8, color=self.palette.ink2,
                                   annotation_clip=False), y)
            for text, y in (("Vitamin D goal", vitd), ("Burn threshold", med)) if y is not None
        ]
        self._place_threshold_labels()

    def _place_threshold_labels(self) -> bool:
        """Nudge the threshold labels apart; returns True if any label moved.

        The two lines can sit only a few pixels apart (e.g. when the dose runs far past
        the burn threshold), so the labels are kept at least one line of text apart and
        clear of the time-axis labels below the panel. Pixel positions depend on the
        layout, so this runs again after every draw (see ``_on_draw``).
        """
        ax = self.ax_dose
        px_per_pt = self.fig.dpi / 72
        gap = 8 * px_per_pt * 1.35  # one line of 8 pt text, in pixels
        floor = ax.transAxes.transform((0, 0))[1] + gap / 2
        line_px = [ax.transData.transform((0, y))[1] for _, y in self._threshold_labels]  # bottom to top
        want = list(line_px)
        if len(want) == 2 and want[1] - want[0] < gap:
            mid = (want[0] + want[1]) / 2
            want = [mid - gap / 2, mid + gap / 2]
        if want:
            lift = max(0.0, floor - want[0])
            want = [w + lift for w in want]
        moved = False
        for (ann, _), line, target in zip(self._threshold_labels, line_px, want):
            dy = round((target - line) / px_per_pt, 1)
            if abs(ann.xyann[1] - dy) > 0.5:
                ann.xyann = (5, dy)
                moved = True
        return moved

    def _on_draw(self, event) -> None:
        # After a resize the layout changes, so threshold label spacing must be re-checked.
        if self._threshold_labels and self._place_threshold_labels():
            self.canvas.draw_idle()

    def _format_time_axis(self, ax) -> None:
        width_px = self.widget.winfo_width() or 800
        ax.xaxis.set_major_locator(mdates.HourLocator(interval=1 if width_px > 760 else 2))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
        ax.xaxis.set_minor_locator(mdates.MinuteLocator(byminute=[30]))

    @staticmethod
    def _to_datetime(x: float) -> datetime:
        dt = mdates.num2date(x).replace(tzinfo=None)
        return (dt + timedelta(seconds=30)).replace(second=0, microsecond=0)

    def _span_selected(self, xmin: float, xmax: float) -> None:
        start, end = self._to_datetime(xmin), self._to_datetime(xmax)
        if end - start >= CLICK_TOLERANCE:  # shorter spans are clicks, handled in _on_release
            self._on_select(start, end)

    def _on_press(self, event) -> None:
        in_chart = event.inaxes in (self.ax_uv, self.ax_dose) and event.xdata is not None
        self._press = (event.x, event.xdata) if (event.button == 1 and in_chart and self._readout) else None

    def _on_release(self, event) -> None:
        press, self._press = self._press, None
        if press and event.button == 1 and abs(event.x - press[0]) <= CLICK_PIXELS:
            self._on_click(self._to_datetime(press[1]))

    def _on_motion(self, event) -> None:
        if not self._readout or not self._view or event.inaxes not in (self.ax_uv, self.ax_dose):
            return
        if event.xdata is None:
            return
        t = self._to_datetime(event.xdata)
        for line in self._hover_lines:
            line.set_xdata([t, t])
            line.set_visible(True)
        if self._view.describe:
            self._readout.set_text(self._view.describe(t))
        self.canvas.draw_idle()

    def _on_leave(self, event) -> None:
        if not self._readout:
            return
        for line in self._hover_lines:
            line.set_visible(False)
        self._readout.set_text(self._hint)
        self.canvas.draw_idle()
