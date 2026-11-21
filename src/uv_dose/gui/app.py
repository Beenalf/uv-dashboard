"""Main window."""

from __future__ import annotations

import csv
import io
import queue
import sys
import threading
import tkinter as tk
import webbrowser
from datetime import date, datetime, time, timedelta
from tkinter import ttk
from typing import Any, Callable

from uv_dose.arpansa import (
    DEFAULT_SITE_CODE,
    DISCLAIMER_URL,
    DOSE_PAGE_URL,
    EARLIEST_DATE,
    FALLBACK_SITES,
    ArpansaClient,
    DayData,
    Site,
    sort_sites,
)
from uv_dose.dose import (
    ONE_MINUTE,
    Source,
    UVPoint,
    build_series,
    cumulative_curve,
    hourly_breakdown,
    minute_dose,
    rate_dose,
    uv_category,
    window_dose,
)
from uv_dose.gui.chart import ChartView, DoseChart
from uv_dose.gui.quiz import FitzpatrickDialog
from uv_dose.gui.theme import LEVEL_ICON, Palette, apply_ttk_theme, fonts, status_color
from uv_dose.gui.widgets import DateControl, ResultCard, TimeSpinbox, fmt_clock, fmt_duration, parse_time
from uv_dose.planner import SunPlan, plan_exposure
from uv_dose.settings import Settings
from uv_dose.skin import SKIN_TYPES, SkinType, skin_type
from uv_dose.vitamin_d import PLATEAU_MED, BODY_EXPOSURES, BodyExposure, body_exposure, vitamin_d_iu

SPF_CHOICES: tuple[tuple[str, int], ...] = (("None", 1), ("SPF 15", 15), ("SPF 30", 30), ("SPF 50+", 50))
AUTO_REFRESH_MS = 120_000
CARDS_ONE_ROW_MIN_WIDTH = 880  # px; below this the result cards wrap into a 2 x 2 grid
APP_TITLE = "UV Dose"


class App:
    def __init__(self, root: tk.Tk, client: ArpansaClient, settings: Settings) -> None:
        self.root = root
        self.client = client
        self.settings = settings
        self.palette: Palette = apply_ttk_theme(root, settings.dark)

        self.sites: list[Site] = sort_sites(FALLBACK_SITES)
        self.site: Site = self._site_by_code(settings.site_code)
        self.day: DayData | None = None
        self._first_load = True
        self._request_id = 0
        self._jobs: queue.Queue[tuple[Callable[[Any], None], Any]] = queue.Queue()
        self._after: dict[str, str] = {}
        self._hour_rows: list = []
        self._last_plan: SunPlan | None = None

        st = skin_type(settings.skin_type if 1 <= settings.skin_type <= 6 else 2)
        self.site_var = tk.StringVar(value=self.site.label)
        self.start_var = tk.StringVar(value=settings.start)
        self.end_var = tk.StringVar(value=settings.end)
        self.skin_var = tk.StringVar(value=st.label)
        self.med_var = tk.StringVar(value=f"{settings.med_sed or st.med_sed:g}")
        self.spf_var = tk.StringVar(value=next((l for l, v in SPF_CHOICES if v == settings.spf), "None"))
        try:
            body = body_exposure(settings.body_key)
        except KeyError:
            body = BODY_EXPOSURES[1]
        self.body_var = tk.StringVar(value=body.label)
        self.target_var = tk.StringVar(value=str(settings.target_iu))
        self.source_var = tk.StringVar(value=settings.source if settings.source in {s.value for s in Source} else "best")
        self.dark_var = tk.BooleanVar(value=settings.dark)
        self.status_var = tk.StringVar(value="")

        self._pending_start_now = False
        self._card_cols = 0

        root.title(f"{APP_TITLE}: personal UV dose from ARPANSA data")
        self._initial_geometry()
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_menu()
        self._build_layout()
        self._apply_palette()

        root.after(80, self._poll_jobs)
        self._load_sites()
        self._load_day()

    # ================================================================== layout

    def _initial_geometry(self) -> None:
        """Size the window for the display's scaling (fonts grow with DPI, so the window must too)."""
        root = self.root
        scale = max(root.winfo_fpixels("1i") / 96, 1.0)
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.minsize(min(round(1120 * scale), sw - 40), min(round(720 * scale), sh - 100))
        w, h = round(1380 * scale), round(880 * scale)
        if w <= sw - 40 and h <= sh - 100:
            root.geometry(f"{w}x{h}")
            return
        try:
            root.state("zoomed")  # Windows and macOS
        except tk.TclError:
            try:
                root.attributes("-zoomed", True)  # X11
            except tk.TclError:
                root.geometry(f"{sw - 40}x{sh - 100}+0+0")

    def _build_menu(self) -> None:
        bar = tk.Menu(self.root)
        file_menu = tk.Menu(bar, tearoff=False)
        file_menu.add_command(label="Copy hourly table as CSV", command=self._copy_csv)
        file_menu.add_command(label="Refresh data", accelerator="F5", command=lambda: self._load_day(force=True))
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self._on_close)
        bar.add_cascade(label="File", menu=file_menu)
        view_menu = tk.Menu(bar, tearoff=False)
        view_menu.add_checkbutton(label="Dark theme", variable=self.dark_var, command=self._toggle_theme)
        bar.add_cascade(label="View", menu=view_menu)
        help_menu = tk.Menu(bar, tearoff=False)
        help_menu.add_command(label="How the calculations work", command=self._show_help)
        help_menu.add_command(label="ARPANSA data disclaimer", command=self._show_disclaimer)
        help_menu.add_separator()
        help_menu.add_command(label="Open ARPANSA's UV dose page", command=lambda: webbrowser.open(DOSE_PAGE_URL))
        bar.add_cascade(label="Help", menu=help_menu)
        self.root.configure(menu=bar)
        self.root.bind("<F5>", lambda e: self._load_day(force=True))

    def _build_layout(self) -> None:
        f = fonts(self.root)
        outer = ttk.Frame(self.root, padding=(16, 12, 16, 8))
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(1, weight=1)

        # ---------------- header
        header = ttk.Frame(outer)
        header.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        header.columnconfigure(5, weight=1)
        ttk.Label(header, text="☀ " + APP_TITLE, style="Header.TLabel").grid(row=0, column=0, padx=(0, 24))
        ttk.Label(header, text="City").grid(row=0, column=1, padx=(0, 6))
        self.site_box = ttk.Combobox(header, textvariable=self.site_var, state="readonly", width=22,
                                     values=[s.label for s in self.sites])
        self.site_box.grid(row=0, column=2, padx=(0, 18))
        self.site_box.bind("<<ComboboxSelected>>", lambda e: self._on_site_change())
        ttk.Label(header, text="Date").grid(row=0, column=3, padx=(0, 6))
        self.date_ctl = DateControl(header, on_change=lambda d: self._load_day())
        self.date_ctl.grid(row=0, column=4, sticky="w")
        ttk.Label(header, textvariable=self.status_var, style="Muted.TLabel", anchor="e", justify="right",
                  wraplength=260).grid(row=0, column=5, sticky="e", padx=(12, 8))
        ttk.Button(header, text="↻ Refresh", command=lambda: self._load_day(force=True)).grid(row=0, column=6)

        # ---------------- sidebar
        side = ttk.Frame(outer, width=300)
        side.grid(row=1, column=0, sticky="nsw", padx=(0, 16))
        side.columnconfigure(1, weight=1)
        r = 0

        def section(title: str) -> None:
            nonlocal r
            ttk.Label(side, text=title, style="Section.TLabel").grid(
                row=r, column=0, columnspan=3, sticky="w", pady=(14 if r else 0, 6))
            r += 1

        def hint(textvar_or_text: str | tk.StringVar) -> ttk.Label:
            nonlocal r
            kw = {"textvariable": textvar_or_text} if isinstance(textvar_or_text, tk.StringVar) else {
                "text": textvar_or_text}
            lbl = ttk.Label(side, style="Muted.TLabel", wraplength=280, justify="left", font=f["caption"], **kw)
            lbl.grid(row=r, column=0, columnspan=3, sticky="w", pady=(2, 0))
            r += 1
            return lbl

        section("Time in the sun")
        times = ttk.Frame(side)
        times.grid(row=r, column=0, columnspan=3, sticky="w")
        ttk.Label(times, text="Start").grid(row=0, column=0, padx=(0, 6))
        TimeSpinbox(times, self.start_var, self._schedule_recompute).grid(row=0, column=1, padx=(0, 14))
        ttk.Label(times, text="End").grid(row=0, column=2, padx=(0, 6))
        TimeSpinbox(times, self.end_var, self._schedule_recompute).grid(row=0, column=3)
        r += 1
        presets = ttk.Frame(side)
        presets.grid(row=r, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Button(presets, text="Start now", command=self._start_now).pack(side="left", padx=(0, 6))
        ttk.Button(presets, text="10:00–15:00", command=lambda: self._set_window(time(10), time(15))).pack(
            side="left", padx=(0, 6))
        ttk.Button(presets, text="Whole day", command=self._whole_day).pack(side="left")
        r += 1
        hint("Or drag across the chart to pick a window. Click the chart to move just the start. "
             "The burn and vitamin D times count from the start time.")

        section("Your skin")
        self.skin_box = ttk.Combobox(side, textvariable=self.skin_var, state="readonly", width=34,
                                     values=[s.label for s in SKIN_TYPES])
        self.skin_box.grid(row=r, column=0, columnspan=3, sticky="ew")
        self.skin_box.bind("<<ComboboxSelected>>", lambda e: self._on_skin_change())
        r += 1
        ttk.Button(side, text="Work out my skin type…", command=self._open_quiz).grid(
            row=r, column=0, columnspan=3, sticky="w", pady=(6, 0))
        r += 1
        med_row = ttk.Frame(side)
        med_row.grid(row=r, column=0, columnspan=3, sticky="w", pady=(10, 0))
        ttk.Label(med_row, text="Burn threshold").pack(side="left", padx=(0, 6))
        med_spin = ttk.Spinbox(med_row, textvariable=self.med_var, from_=0.5, to=30, increment=0.5, width=6,
                               command=self._schedule_recompute)
        med_spin.pack(side="left")
        med_spin.bind("<Return>", lambda e: self._schedule_recompute())
        med_spin.bind("<FocusOut>", lambda e: self._schedule_recompute())
        ttk.Label(med_row, text="SED").pack(side="left", padx=(6, 0))
        r += 1
        self.med_hint = tk.StringVar()
        hint(self.med_hint)

        section("Sunscreen and vitamin D")
        ttk.Label(side, text="Sunscreen").grid(row=r, column=0, sticky="w", pady=3)
        spf_box = ttk.Combobox(side, textvariable=self.spf_var, state="readonly", width=20,
                               values=[l for l, _ in SPF_CHOICES])
        spf_box.grid(row=r, column=1, columnspan=2, sticky="ew", pady=3)
        spf_box.bind("<<ComboboxSelected>>", lambda e: self._schedule_recompute())
        r += 1
        ttk.Label(side, text="Skin exposed").grid(row=r, column=0, sticky="w", pady=3, padx=(0, 8))
        body_box = ttk.Combobox(side, textvariable=self.body_var, state="readonly", width=26,
                                values=[b.label for b in BODY_EXPOSURES])
        body_box.grid(row=r, column=1, columnspan=2, sticky="ew", pady=3)
        body_box.bind("<<ComboboxSelected>>", lambda e: self._schedule_recompute())
        r += 1
        ttk.Label(side, text="Vitamin D goal").grid(row=r, column=0, sticky="w", pady=3)
        tgt = ttk.Frame(side)
        tgt.grid(row=r, column=1, columnspan=2, sticky="w", pady=3)
        tspin = ttk.Spinbox(tgt, textvariable=self.target_var, from_=200, to=4000, increment=100, width=7,
                            command=self._schedule_recompute)
        tspin.pack(side="left")
        tspin.bind("<Return>", lambda e: self._schedule_recompute())
        tspin.bind("<FocusOut>", lambda e: self._schedule_recompute())
        ttk.Label(tgt, text="IU").pack(side="left", padx=(6, 0))
        r += 1
        hint("Sunscreen as labelled; most people apply less, so real protection is often lower.")

        section("UV data")
        ttk.Radiobutton(side, text="Measured (forecast fills any gaps)", value=Source.BEST.value,
                        variable=self.source_var, command=self._schedule_recompute).grid(
            row=r, column=0, columnspan=3, sticky="w", pady=2)
        r += 1
        ttk.Radiobutton(side, text="Clear-sky forecast (cloudless day)", value=Source.CLEAR_SKY.value,
                        variable=self.source_var, command=self._schedule_recompute).grid(
            row=r, column=0, columnspan=3, sticky="w", pady=2)
        r += 1

        # ---------------- main area
        main = ttk.Frame(outer)
        main.grid(row=1, column=1, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(2, weight=1)

        cards = ttk.Frame(main)
        cards.grid(row=0, column=0, sticky="ew")
        self._cards_frame = cards
        self.cards: dict[str, ResultCard] = {}
        for key, title in (("dose", "UV dose"), ("burn", "Time to burn"), ("vitd", "Vitamin D"), ("uv", "Peak UV")):
            self.cards[key] = ResultCard(cards, title, self.palette)
        self._layout_cards(4)
        cards.bind("<Configure>", lambda e: self._layout_cards(4 if e.width >= CARDS_ONE_ROW_MIN_WIDTH else 2))

        self.note_var = tk.StringVar()
        ttk.Label(main, textvariable=self.note_var, style="Muted.TLabel", font=f["caption"]).grid(
            row=1, column=0, sticky="w", pady=(6, 4))

        self.tabs = ttk.Notebook(main)
        self.tabs.grid(row=2, column=0, sticky="nsew")
        chart_tab = ttk.Frame(self.tabs)
        self.tabs.add(chart_tab, text="Chart")
        self.chart = DoseChart(chart_tab, self.palette, on_select=self._on_chart_select,
                               on_click=self._on_chart_click)
        self.chart.widget.pack(fill="both", expand=True)

        table_tab = ttk.Frame(self.tabs, padding=8)
        self.tabs.add(table_tab, text="Hourly table")
        table_tab.columnconfigure(0, weight=1)
        table_tab.rowconfigure(1, weight=1)
        bar = ttk.Frame(table_tab)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        self.table_caption = tk.StringVar()
        ttk.Label(bar, textvariable=self.table_caption, style="Muted.TLabel").pack(side="left")
        ttk.Button(bar, text="Copy as CSV", command=self._copy_csv).pack(side="right")
        cols = ("time", "mean", "peak", "sed", "cum", "skin", "data")
        self.table = ttk.Treeview(table_tab, columns=cols, show="headings", height=12)
        for col, text, w, anchor in (
            ("time", "Time", 120, "w"), ("mean", "Mean UV", 80, "e"), ("peak", "Peak UV", 80, "e"),
            ("sed", "Dose (SED)", 90, "e"), ("cum", "Running total (SED)", 140, "e"),
            ("skin", "On your skin (SED)", 140, "e"), ("data", "   Data", 220, "w"),
        ):
            self.table.heading(col, text=text, anchor=anchor)
            self.table.column(col, width=w, anchor=anchor, stretch=col == "data")
        self.table.grid(row=1, column=0, sticky="nsew")
        sb = ttk.Scrollbar(table_tab, orient="vertical", command=self.table.yview)
        sb.grid(row=1, column=1, sticky="ns")
        self.table.configure(yscrollcommand=sb.set)

        # ---------------- footer (ARPANSA's conditions of use: attribution + disclaimer link)
        footer = ttk.Frame(outer)
        footer.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Label(footer, text="UV observations courtesy of ARPANSA.", style="Muted.TLabel",
                  font=f["caption"]).pack(side="left")
        self._links: list[ttk.Label] = []
        for text, cmd in (("Data disclaimer", self._show_disclaimer), ("How the calculations work", self._show_help)):
            link = ttk.Label(footer, text=text, cursor="hand2", font=f["caption"], foreground=self.palette.series)
            link.pack(side="left", padx=(12, 0))
            link.bind("<Button-1>", lambda e, c=cmd: c())
            self._links.append(link)
        ttk.Label(footer, text="Not medical advice. Sunburn can take 16–24 hours to show.",
                  style="Muted.TLabel", font=f["caption"]).pack(side="right")
        self._update_med_hint()

    def _layout_cards(self, cols: int) -> None:
        """One row of four cards, or a 2 x 2 grid when the window is too narrow for four."""
        if cols == self._card_cols:
            return
        self._card_cols = cols
        for c in range(4):
            self._cards_frame.columnconfigure(c, weight=1 if c < cols else 0, uniform="card" if c < cols else "")
        for i, card in enumerate(self.cards.values()):
            r, c = divmod(i, cols)
            card.grid(row=r, column=c, sticky="nsew", padx=(0 if c == 0 else 10, 0), pady=(0 if r == 0 else 10, 0))
            card.set_compact(cols < 4)  # two rows of cards: drop the fine print to keep the chart usable

    # ================================================================== theme

    def _apply_palette(self) -> None:
        for card in self.cards.values():
            card.apply_palette(self.palette)
        for link in self._links:
            link.configure(foreground=self.palette.series)
        self.chart.set_palette(self.palette)

    def _toggle_theme(self) -> None:
        self.palette = apply_ttk_theme(self.root, self.dark_var.get())
        self._apply_palette()
        self._recompute()
        self._save_settings_soon()

    # ================================================================== background jobs

    def _submit(self, fn: Callable[[], Any], on_ok: Callable[[Any], None], on_err: Callable[[Exception], None]) -> None:
        def worker() -> None:
            try:
                result = fn()
            except Exception as exc:  # noqa: BLE001 - surfaced to the user
                self._jobs.put((on_err, exc))
            else:
                self._jobs.put((on_ok, result))

        threading.Thread(target=worker, daemon=True).start()

    def _poll_jobs(self) -> None:
        try:
            while True:
                callback, arg = self._jobs.get_nowait()
                callback(arg)
        except queue.Empty:
            pass
        self.root.after(80, self._poll_jobs)

    def _later(self, key: str, ms: int, fn: Callable[[], None]) -> None:
        if key in self._after:
            self.root.after_cancel(self._after[key])
        self._after[key] = self.root.after(ms, fn)

    # ================================================================== data loading

    def _site_by_code(self, code: str) -> Site:
        for s in self.sites:
            if s.code == code:
                return s
        return next((s for s in self.sites if s.code == DEFAULT_SITE_CODE), self.sites[0])

    def _load_sites(self) -> None:
        def ok(sites: list[Site]) -> None:
            self.sites = sites
            self.site_box.configure(values=[s.label for s in sites])
            current = self._site_by_code(self.site.code)
            if current != self.site:
                self.site = current
                self.site_var.set(current.label)

        self._submit(self.client.sites, ok, lambda exc: None)  # the built-in list keeps working offline

    def _on_site_change(self) -> None:
        label = self.site_var.get()
        self.site = next((s for s in self.sites if s.label == label), self.site)
        self._load_day()
        self._save_settings_soon()

    def _load_day(self, force: bool = False, quiet: bool = False) -> None:
        d = self.date_ctl.value
        site = self.site
        if d < EARLIEST_DATE:
            self.day = None
            self._show_message(f"ARPANSA's minute-by-minute UV records start in {EARLIEST_DATE.year}.\n"
                               "Pick a later date.")
            return
        self._request_id += 1
        rid = self._request_id
        if not quiet:
            self.status_var.set(f"Loading {site.name}…")

        def ok(data: DayData) -> None:
            if rid != self._request_id:
                return  # a newer request superseded this one
            self.day = data
            self._on_day_loaded()

        def err(exc: Exception) -> None:
            if rid != self._request_id:
                return
            self._pending_start_now = False
            self.status_var.set(f"⚠ {str(exc).split(' (')[0]}")  # full reason goes on the chart
            if self.day is None or self.day.site != site or self.day.day != d:
                self.day = None
                self._show_message(f"Couldn't load UV data: {exc}\nCheck your connection, then press Refresh (F5).")

        self._submit(lambda: self.client.day(site, d, force=force), ok, err)

    def _on_day_loaded(self) -> None:
        day = self.day
        assert day is not None
        if not day.has_data:
            self.status_var.set("")
            if day.day >= date.today():
                self._show_message(f"No UV data yet for {day.site.name}.\nTry another city.")
            else:
                self._show_message(f"No UV data for {day.site.name} on this date.\n"
                                   "Records start later at some sites. Try a more recent date or another city.")
            return
        latest = day.latest_measured
        live = not day.is_complete and day.day == date.today()
        if live and latest:
            self.status_var.set(f"Live · latest reading {latest.time:%H:%M}")
            self._later("refresh", AUTO_REFRESH_MS, lambda: self._load_day(quiet=True))
        elif day.has_measurements:
            self.status_var.set("Measured data")
        else:
            self.status_var.set("Clear-sky forecast only")

        if self._first_load:
            self._first_load = False
            if live and latest and latest.time.hour < 18:
                s = latest.time.replace(minute=latest.time.minute - latest.time.minute % 5)
                self._set_window(s.time(), min(s + timedelta(hours=2), day.end_time).time(), recompute=False)
        if self._pending_start_now and day.day == date.today():
            self._pending_start_now = False
            self._start_now()  # "Start now" was pressed on another day: apply it now today has loaded
        self._recompute()

    def _show_message(self, message: str) -> None:
        d = self.date_ctl.value
        start = datetime.combine(d, time(6))
        self.chart.render(ChartView(title=f"{self.site.name} · {d:%a %d %b %Y}", day_start=start,
                                    day_end=start + timedelta(hours=14), message=message))
        for card in self.cards.values():
            card.show("–")
        self.note_var.set("")
        self.table.delete(*self.table.get_children())

    # ================================================================== inputs

    def _skin(self) -> SkinType:
        label = self.skin_var.get()
        return next((s for s in SKIN_TYPES if s.label == label), SKIN_TYPES[1])

    def _med(self) -> float:
        try:
            v = float(self.med_var.get())
            if 0.1 <= v <= 100:
                return v
        except ValueError:
            pass
        return self._skin().med_sed

    def _spf(self) -> int:
        return dict(SPF_CHOICES).get(self.spf_var.get(), 1)

    def _body(self) -> BodyExposure:
        return next((b for b in BODY_EXPOSURES if b.label == self.body_var.get()), BODY_EXPOSURES[1])

    def _target_iu(self) -> int:
        try:
            return max(50, min(int(float(self.target_var.get())), 20000))
        except ValueError:
            return 1000

    def _window(self, day: DayData) -> tuple[datetime, datetime]:
        first, last = day.first_time, day.end_time
        assert first and last
        st = parse_time(self.start_var.get()) or time(10)
        et = parse_time(self.end_var.get()) or time(14)
        start = max(datetime.combine(day.day, st), first)
        end = min(datetime.combine(day.day, et), last)
        if start >= last:
            start = last - timedelta(minutes=30)
        if end <= start:
            end = min(start + timedelta(hours=1), last)
        self.start_var.set(fmt_clock(start))
        self.end_var.set(fmt_clock(end))
        return start, end

    def _set_window(self, start: time, end: time, recompute: bool = True) -> None:
        self.start_var.set(fmt_clock(start))
        self.end_var.set(fmt_clock(end))
        if recompute:
            self._schedule_recompute()

    def _start_now(self) -> None:
        day = self.day
        if not day or day.day != date.today():
            self._pending_start_now = True  # applied when today's data arrives
            if self.date_ctl.value != date.today():
                self.date_ctl.today()
            else:
                self._load_day()
            return
        if day.is_complete or not day.latest_measured:
            self.status_var.set("No more UV readings today")
            return
        now = day.latest_measured.time
        st, et = parse_time(self.start_var.get()), parse_time(self.end_var.get())
        length = (datetime.combine(day.day, et) - datetime.combine(day.day, st)) if st and et else timedelta(hours=1)
        length = max(length, timedelta(minutes=30))
        self._set_window(now.time(), min(now + length, day.end_time).time())

    def _whole_day(self) -> None:
        if self.day and self.day.first_time:
            self._set_window(self.day.first_time.time(), time(23, 59))
        else:
            self._set_window(time(6), time(20))

    def _on_chart_select(self, start: datetime, end: datetime) -> None:
        self._set_window(start.time(), end.time())

    def _on_chart_click(self, t: datetime) -> None:
        st, et = parse_time(self.start_var.get()), parse_time(self.end_var.get())
        if st and et and t.time() < et:
            self._set_window(t.time(), et)
        else:
            length = (datetime.combine(t.date(), et) - datetime.combine(t.date(), st)) if st and et else None
            length = length if length and length > timedelta(0) else timedelta(hours=1)
            self._set_window(t.time(), (t + length).time() if (t + length).date() == t.date() else time(23, 59))

    def _on_skin_change(self) -> None:
        self.med_var.set(f"{self._skin().med_sed:g}")
        self._schedule_recompute()

    def _open_quiz(self) -> None:
        dlg = FitzpatrickDialog(self.root, self.palette, self.settings.quiz_answers)
        self.root.wait_window(dlg)
        if dlg.result:
            st, answers = dlg.result
            self.settings.quiz_answers = answers
            self.skin_var.set(st.label)
            self._on_skin_change()
            self.status_var.set(f"Skin type set to Type {st.roman}")

    def _update_med_hint(self) -> None:
        st = self._skin()
        med = self._med()
        custom = abs(med - st.med_sed) > 1e-9
        text = f"Type {st.roman} default: {st.med_sed:g} SED (published range {st.med_range})."
        if custom:
            text = "Custom threshold. " + text
        self.med_hint.set(text)

    # ================================================================== calculations -> UI

    def _schedule_recompute(self) -> None:
        self._later("recompute", 120, self._recompute)

    def _recompute(self) -> None:
        self._update_med_hint()
        day = self.day
        if not day or not day.has_data:
            return
        source = Source(self.source_var.get())
        series = build_series(day, source)
        start, end = self._window(day)
        skin, med, spf, body, target = self._skin(), self._med(), self._spf(), self._body(), self._target_iu()

        wd = window_dose(series, start, end)
        eff = wd.sed / spf
        rating = rate_dose(eff, med)
        plan = plan_exposure(series, start, med_sed=med, spf=spf, target_iu=target, body_fraction=body.fraction)
        self._last_plan = plan
        clear = None
        if source is Source.BEST and day.has_forecast and wd.minutes > wd.forecast_minutes:
            clear_series = build_series(day, Source.CLEAR_SKY)
            clear = (clear_series, window_dose(clear_series, start, end))

        self._fill_cards(day, series, start, end, wd, eff, rating, plan, clear, skin, med, spf, body, target)
        self._fill_note(day, source, wd)
        self._fill_chart(day, series, source, start, end, spf, med, plan, clear)
        self._fill_table(series, start, end, spf)
        self._save_settings_soon()

    def _fill_cards(self, day, series, start, end, wd, eff, rating, plan: SunPlan, clear, skin, med, spf, body,
                    target) -> None:
        p = self.palette
        span = f"{start:%H:%M}–{end:%H:%M}"

        # ---- dose
        line1 = f"{wd.joules_per_m2:,.0f} J/m² over {fmt_duration(wd.duration)}"
        if spf > 1:
            line1 += f" · {eff:.2f} SED on skin with SPF {spf}"
        extra = [rating.advice]
        if clear and clear[1].sed > 0.05:
            ratio = wd.sed / clear[1].sed
            if ratio < 0.95:
                extra.append(f"Cloud cut this by {1 - ratio:.0%} (clear sky: {clear[1].sed:.1f} SED).")
            elif ratio > 1.05:
                extra.append(f"{ratio - 1:.0%} above the clear-sky forecast ({clear[1].sed:.1f} SED).")
            else:
                extra.append("About the same as a clear-sky day.")
        if wd.forecast_minutes and Source(self.source_var.get()) is Source.BEST:
            extra.append(f"{wd.forecast_minutes} min from the clear-sky forecast.")
        self.cards["dose"].show(
            f"{wd.sed:.2f}", "SED", line1,
            f"{rating.label} · {rating.fraction_of_med:.0%} of your burn threshold",
            " ".join(extra), color=status_color(p, rating.level), icon=LEVEL_ICON[rating.level],
            title=f"UV dose · {span}",
        )

        # ---- burn
        who = f"Type {skin.roman}, {med:g} SED" + (f", SPF {spf}" if spf > 1 else "")
        fc_note = "Includes clear-sky forecast; cloud would make it later." if plan.relies_on_forecast else ""
        if plan.burn_at:
            inside = plan.burn_at <= end
            self.cards["burn"].show(
                fmt_duration(plan.burn_after), "", f"Burn threshold at {plan.burn_at:%H:%M} ({who})",
                "Inside your window: cover up or find shade by then."
                if inside else f"Your window ends {fmt_duration(plan.burn_at - end)} before that.",
                fc_note, color=p.critical if inside else p.good,
                icon=LEVEL_ICON["very_high"] if inside else LEVEL_ICON["low"],
                title=f"Time to burn · from {start:%H:%M}",
            )
        else:
            self.cards["burn"].show(
                "Not today", "", f"Only {plan.dose_available:.1f} SED left after {start:%H:%M} ({who})",
                "Stays under your burn threshold for the rest of the day.", fc_note,
                color=p.good, icon=LEVEL_ICON["low"], title=f"Time to burn · from {start:%H:%M}",
            )

        # ---- vitamin D
        body_short = body.label.split(" (")[0].lower()
        window_iu = vitamin_d_iu(eff, med, body.fraction)
        capped = eff >= med * PLATEAU_MED
        line3 = f"Whole window: ≈{window_iu:,.0f} IU" + (" (production levels off)." if capped else ".")
        if plan.vitd_dose_sed is None:
            self.cards["vitd"].show(
                "Not safely", "", f"{target:,} IU would take more than a burning dose on {body_short}.",
                "Expose more skin rather than staying out longer.", line3,
                color=p.warning, icon=LEVEL_ICON["moderate"], title=f"Vitamin D · from {start:%H:%M}",
            )
        elif plan.vitd_at:
            margin = (f"Sweet spot, well before your burn time ({fmt_duration(plan.burn_after)})."
                      if plan.burn_after else "Sweet spot, with no burn risk today.")
            self.cards["vitd"].show(
                fmt_duration(plan.vitd_after), "", f"≈{target:,} IU by {plan.vitd_at:%H:%M} with {body_short}",
                margin, "After that, more sun mostly adds UV damage. " + line3,
                color=p.good, icon=LEVEL_ICON["low"], title=f"Vitamin D · from {start:%H:%M}",
            )
        else:
            possible = vitamin_d_iu(plan.dose_available, med, body.fraction)
            self.cards["vitd"].show(
                "Not today", "", f"UV after {start:%H:%M} is too weak to reach {target:,} IU",
                f"About {possible:,.0f} IU possible with {body_short}. Try an earlier start.", line3,
                color=p.warning, icon=LEVEL_ICON["moderate"], title=f"Vitamin D · from {start:%H:%M}",
            )

        # ---- UV
        cat = uv_category(wd.peak_uvi)
        protect = [pt.time for pt in series if pt.uvi >= 2.5]
        prot_line = (f"UV 3+ today: {protect[0]:%H:%M}–{protect[-1] + ONE_MINUTE:%H:%M}" if protect
                     else "UV stays below 3 all day.")
        self.cards["uv"].show(
            f"{wd.peak_uvi:.1f}", cat.name,
            f"at {wd.peak_time:%H:%M} · average {wd.mean_uvi:.1f}" if wd.peak_time else "No readings",
            cat.advice, prot_line, color=cat.color, icon="●", title=f"Peak UV · {span}",
        )

    def _fill_note(self, day: DayData, source: Source, wd) -> None:
        latest = day.latest_measured
        if source is Source.CLEAR_SKY:
            self.note_var.set("Showing ARPANSA's clear-sky forecast: the most UV a cloudless day would bring.")
        elif not day.has_measurements:
            self.note_var.set("No measurements for this day, so ARPANSA's clear-sky forecast is used: "
                              "the most UV a cloudless day would bring.")
        elif not day.is_complete and latest and day.day == date.today():
            self.note_var.set(
                f"Measured by ARPANSA until {latest.time:%H:%M}. After that the clear-sky forecast is used, "
                "so real UV may be lower if cloud comes over.")
        elif wd.forecast_minutes:
            self.note_var.set(f"{wd.forecast_minutes} minutes in this window have no measurement, "
                              "so ARPANSA's clear-sky forecast fills them.")
        elif wd.missing_minutes:
            self.note_var.set(f"{wd.missing_minutes} minutes in this window have no data and count as zero.")
        else:
            self.note_var.set(f"Measured by ARPANSA at {day.site.name}, one reading per minute.")

    def _fill_chart(self, day, series: list[UVPoint], source, start, end, spf, med, plan: SunPlan, clear) -> None:
        latest = day.latest_measured
        live = not day.is_complete and latest is not None
        curve = cumulative_curve(series, start, end, spf)
        tail: tuple[list, list] = ([], [])
        if end < day.end_time:
            tt, tv = cumulative_curve(series, end, day.end_time, spf)
            base = curve[1][-1]
            tail = (tt, [base + v for v in tv])
        clear_curve = cumulative_curve(clear[0], start, end, spf) if clear else ([], [])

        # prefix sums for the hover readout
        index = {pt.time: i for i, pt in enumerate(series)}
        prefix = [0.0]
        for pt in series:
            prefix.append(prefix[-1] + minute_dose(pt.uvi, spf))
        start_i = index.get(start, 0 if start <= series[0].time else len(series))
        samples = {s.time: s for s in day.samples}

        def describe(t: datetime) -> str:
            s = samples.get(t)
            if s is None:
                return f"{t:%H:%M}"
            parts = [f"{t:%H:%M}"]
            if s.measured is not None:
                parts.append(f"UV {s.measured:.1f} ({uv_category(s.measured).name.lower()})")
            if s.forecast is not None:
                parts.append(f"clear sky {s.forecast:.1f}")
            if t > start and t in index:
                dose = prefix[index[t]] - prefix[start_i]  # dose from start up to time t
                parts.append(f"{dose:.2f} SED on skin since {start:%H:%M}")
            return "  ·  ".join(parts)

        self.chart.render(ChartView(
            title=f"{day.site.name} · {day.day:%a %d %b %Y}",
            day_start=day.first_time,
            day_end=day.end_time,
            times=[s.time for s in day.samples],
            measured=[s.measured for s in day.samples],
            forecast=[s.forecast for s in day.samples],
            show_measured=source is Source.BEST and day.has_measurements,
            latest_reading=latest.time if live else None,
            window_start=start,
            window_end=end,
            curve=curve,
            tail=tail,
            clear_curve=clear_curve,
            dose_label="Dose on your skin" + (f" (SPF {spf})" if spf > 1 else ""),
            med_sed=med,
            vitd_sed=plan.vitd_dose_sed,
            burn_at=plan.burn_at,
            vitd_at=plan.vitd_at,
            describe=describe,
        ))

    def _fill_table(self, series, start, end, spf) -> None:
        self.table.delete(*self.table.get_children())
        rows = hourly_breakdown(series, start, end)
        self._hour_rows = rows
        for row in rows:
            notes = []
            if row.forecast_minutes:
                notes.append(f"clear-sky forecast {row.forecast_minutes} min")
            if row.missing_minutes:
                notes.append(f"no data {row.missing_minutes} min")
            self.table.insert("", "end", values=(
                f"{row.start:%H:%M}–{row.end:%H:%M}", f"{row.mean_uvi:.1f}", f"{row.peak_uvi:.1f}",
                f"{row.sed:.2f}", f"{row.cumulative_sed:.2f}", f"{row.cumulative_sed / spf:.2f}",
                "   " + (", ".join(notes) or "measured"),
            ))
        total = rows[-1].cumulative_sed if rows else 0.0
        self.table_caption.set(
            f"{self.site.name}, {start:%d/%m/%Y} {start:%H:%M}–{end:%H:%M}: {total:.2f} SED in total "
            f"({total * 100:,.0f} J/m²). Partial hours at the edges of the window."
        )

    # ================================================================== misc actions

    def _copy_csv(self) -> None:
        if not self._hour_rows:
            self.status_var.set("Nothing to copy yet")
            return
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(["site", "date", "from", "to", "minutes", "mean_uvi", "peak_uvi", "dose_sed",
                    "running_total_sed", "forecast_minutes", "missing_minutes"])
        for r in self._hour_rows:
            w.writerow([self.site.name, r.start.date().isoformat(), f"{r.start:%H:%M}", f"{r.end:%H:%M}",
                        r.minutes, f"{r.mean_uvi:.2f}", f"{r.peak_uvi:.2f}", f"{r.sed:.3f}",
                        f"{r.cumulative_sed:.3f}", r.forecast_minutes, r.missing_minutes])
        self.root.clipboard_clear()
        self.root.clipboard_append(buf.getvalue())
        self.status_var.set("Table copied as CSV")

    def _text_dialog(self, title: str, body: str, buttons: tuple[tuple[str, Callable[[], None]], ...] = ()) -> None:
        p = self.palette
        dlg = tk.Toplevel(self.root)
        dlg.title(title)
        dlg.transient(self.root)
        dlg.configure(background=p.window)
        frame = ttk.Frame(dlg, padding=16)
        frame.pack(fill="both", expand=True)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        text = tk.Text(frame, wrap="word", width=92, height=30, relief="flat", padx=12, pady=10,
                       background=p.surface, foreground=p.ink, font=fonts(self.root)["body"],
                       highlightthickness=1, highlightbackground=p.border, spacing1=2, spacing3=4)
        text.tag_configure("h", font=fonts(self.root)["strong"], spacing1=10)
        for line in body.strip("\n").splitlines():
            if line.startswith("## "):
                text.insert("end", line[3:] + "\n", "h")
            else:
                text.insert("end", line + "\n")
        text.configure(state="disabled")
        text.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        sb.grid(row=0, column=1, sticky="ns")
        text.configure(yscrollcommand=sb.set)
        bar = ttk.Frame(frame)
        bar.grid(row=1, column=0, columnspan=2, sticky="e", pady=(12, 0))
        for label, cmd in buttons:
            ttk.Button(bar, text=label, command=cmd).pack(side="left", padx=(0, 6))
        ttk.Button(bar, text="Close", style="Accent.TButton", command=dlg.destroy).pack(side="left")
        dlg.bind("<Escape>", lambda e: dlg.destroy())

    def _show_disclaimer(self) -> None:
        self._text_dialog("ARPANSA data disclaimer", DISCLAIMER_TEXT,
                          (("Open ARPANSA developer page", lambda: webbrowser.open(DISCLAIMER_URL)),))

    def _show_help(self) -> None:
        self._text_dialog("How the calculations work", HELP_TEXT)

    # ================================================================== settings

    def _save_settings_soon(self) -> None:
        self._later("save", 1500, self._save_settings)

    def _save_settings(self) -> None:
        s = self.settings
        s.site_code = self.site.code
        s.skin_type = self._skin().number
        med = self._med()
        s.med_sed = None if abs(med - self._skin().med_sed) < 1e-9 else med
        s.spf = self._spf()
        s.body_key = self._body().key
        s.target_iu = self._target_iu()
        s.source = self.source_var.get()
        s.dark = self.dark_var.get()
        s.start, s.end = self.start_var.get(), self.end_var.get()
        s.save()

    def _on_close(self) -> None:
        self._save_settings()
        self.root.destroy()


DISCLAIMER_TEXT = """
## Source
UV observations courtesy of ARPANSA (Australian Radiation Protection and Nuclear Safety Agency).

## ARPANSA's disclaimer
This data is presented by ARPANSA for the purpose of disseminating information about solar ultraviolet radiation levels, free of charge, for the benefit of the public. Nothing contained on this website, or the data presented, is intended to be used as a substitute for a health professional's advice. ARPANSA does not accept any liability for any injury, loss, damage or costs incurred, or any consequences resulting directly or indirectly by use of or reliance on the information.

ARPANSA updates the information regularly and makes every effort to ensure the quality of the information subject to scientific and technical uncertainties. Before relying on the information, users should carefully evaluate its accuracy, currency, completeness and relevance for their purposes, and should obtain any appropriate professional advice relevant to their particular circumstances. ARPANSA cannot guarantee and assumes no legal liability or responsibility for the accuracy, currency or completeness of the information.

## About this app
The burn, skin type and vitamin D figures are estimates from simple published models. People vary a lot, so treat them as a guide and not as medical advice.
"""

HELP_TEXT = """
## Where the numbers come from
ARPANSA measures erythemal (skin-reddening) UV once a minute at 16 sites and publishes it as a UV Index. This app downloads a day of those minute-by-minute readings and adds up the minutes in the window you choose. ARPANSA's own dose page only shows whole SEDs per clock hour.

## Standard Erythemal Dose (SED)
1 UV Index = 25 mW/m² of erythemally weighted UV, and 1 SED = 100 J/m². So one minute at UV Index u gives 0.015 × u SED. For example, an hour at UV 10 is 9 SED. Adding up a whole day this way reproduces ARPANSA's daily totals.
The dose is "ambient": what a flat, unshaded surface facing the sky receives. Arms, faces and backs mostly face other directions, and shade, hats and clothing cut exposure further, so the dose on your skin is usually lower.
ARPANSA's guidance: "A very fair-skinned person can receive sunburn and skin damage from only two SEDs." "One SED per day is considered safe for most people. You should consider taking sun protection measures if you could be subjected to a UV dose of more than one SED, especially if you have fair skin."

## Dose rating
The rating compares the dose on your skin (after sunscreen) with your burn threshold: Low is under 50%, Moderate is 50–100%, High is 100–200% (sunburn likely) and Very high is 200% or more.

## Skin type and burn threshold
The questionnaire is ARPANSA's Fitzpatrick skin phototype sheet: 10 questions scored 0–4. A total of 0–6 is Type I, 7–13 Type II, 14–20 Type III, 21–27 Type IV, 28–34 Type V and 35 or more Type VI.
The burn threshold (minimal erythemal dose, MED) defaults to 2, 2.5, 3, 4.5, 6 and 10 SED for Types I–VI. This is a widely used reference set at or near the cautious end of published ranges. Individuals vary, so you can override it.

## Time to burn
This is the time from your start until the dose on your skin reaches your burn threshold, following the real UV curve rather than assuming constant UV. Sunscreen divides the dose by its SPF. SPF ratings assume a thick layer (2 mg/cm²); most people apply far less, so real protection is often much lower than the label.

## Vitamin D
This uses Holick's rule of thumb, as adopted by Webb & Engelsen (2006): exposing about a quarter of the body (face, arms and hands) to a quarter of your burn threshold makes roughly 1000 IU. In general:
    IU ≈ 16,000 × (fraction of skin exposed) × (dose / burn threshold), capped at 1 burn-threshold dose.
Vitamin D production levels off with continued exposure, so short, regular exposure beats long sessions. The "sweet spot" is the time needed to reach your goal. Beyond it, extra sun mostly adds UV damage.
Caveats: the model treats erythemal UV as a stand-in for vitamin-D-producing UV. That overstates production when the sun is low (early, late and in winter). It ignores age, body size and diet. Daily goals of 600–1000 IU are commonly used.

## Measured vs clear-sky forecast
"Measured" uses ARPANSA's readings, and for any minute without one (for example later today) it uses ARPANSA's clear-sky forecast. "Clear-sky forecast" shows the most UV a cloudless day would bring. That is a cautious figure for sunburn and an optimistic one for vitamin D.

## Sun protection
Cancer Council and ARPANSA recommend sun protection whenever the UV Index is 3 or above. Sunburn can take 16–24 hours to show fully.
"""


def run() -> None:
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    App(root, ArpansaClient(), Settings.load())
    root.mainloop()
