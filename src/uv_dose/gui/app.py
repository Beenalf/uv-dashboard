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
    MAX_DAYS_AHEAD,
    ArpansaClient,
    DayData,
    Site,
    sort_sites,
)
from uv_dose.dose import (
    ProtectionTimes,
    Source,
    UVPoint,
    build_series,
    rate_dose,
    sun_protection_times,
    uv_category,
    window_dose,
)
from uv_dose.exposure import (
    APPLICATION_CHOICES,
    DEFAULT_APPLICATION,
    REAPPLY_HOURS,
    SHADE_CHOICES,
    SURFACE_CHOICES,
    Choice,
    choice,
    effective_spf,
    exposure_factor,
)
from uv_dose.gui.chart import ChartView, DoseChart
from uv_dose.gui.outings import MAX_OUTINGS, OutingList, OutingSpec
from uv_dose.gui.quiz import FitzpatrickDialog
from uv_dose.gui.theme import LEVEL_ICON, Palette, apply_ttk_theme, fonts, status_color
from uv_dose.gui.widgets import (
    DateControl,
    ResultCard,
    ScrollableFrame,
    TimeSpinbox,
    fmt_clock,
    fmt_duration,
    parse_time,
)
from uv_dose.planner import DayPlan, Outing, plan_curves, plan_day, plan_hours, value_at
from uv_dose.settings import Settings
from uv_dose.skin import SKIN_TYPES, SkinType, skin_type
from uv_dose.vitamin_d import (
    BODY_EXPOSURES,
    PLATEAU_MED,
    BodyExposure,
    body_exposure,
    most_before_burning,
    next_goal,
    vitamin_d_iu,
)

SPF_CHOICES: tuple[tuple[str, int], ...] = (("No sunscreen", 1), ("SPF 15", 15), ("SPF 30", 30), ("SPF 50+", 50))
SPF_OFF_LABEL = "Off (Applied is None)"  # shown instead of the outing's SPF while sunscreen is turned off
AUTO_REFRESH_MS = 120_000
RECOMPUTE_DELAY_MS = 50  # lets a burst of changes (e.g. a held spinbox arrow) update the results once
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

        # The day's outings (times of day). One is selected: the sidebar fields and the
        # chart's drag and click edit that one.
        self.outings: list[OutingSpec] = [o for o in map(OutingSpec.from_dict, settings.outings) if o][:MAX_OUTINGS]
        if not self.outings:
            self.outings = [OutingSpec(time(10), time(14))]
        self.outings.sort(key=lambda o: (o.start, o.end))
        self.sel: OutingSpec = self.outings[0]
        self._resolved: list[tuple[OutingSpec, Outing]] = []  # the outings on the shown day, in time order

        st = skin_type(settings.skin_type if 1 <= settings.skin_type <= 6 else 2)
        self.site_var = tk.StringVar(value=self.site.label)
        self.start_var = tk.StringVar()
        self.end_var = tk.StringVar()
        self.shade_var = tk.StringVar()
        self.surface_var = tk.StringVar()
        self.spf_var = tk.StringVar()
        self._push_selected()
        self.skin_var = tk.StringVar(value=st.label)
        self.med_var = tk.StringVar(value=f"{settings.med_sed or st.med_sed:g}")
        self.application_var = tk.StringVar(value=choice(APPLICATION_CHOICES, settings.application).label)
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
        self.date_ctl = DateControl(header, on_change=lambda d: self._load_day(), max_days_ahead=MAX_DAYS_AHEAD)
        self.date_ctl.grid(row=0, column=4, sticky="w")
        ttk.Label(header, textvariable=self.status_var, style="Muted.TLabel", anchor="e", justify="right",
                  wraplength=260).grid(row=0, column=5, sticky="e", padx=(12, 8))
        ttk.Button(header, text="↻ Refresh", command=lambda: self._load_day(force=True)).grid(row=0, column=6)

        # ---------------- sidebar
        # The sidebar scrolls when it's taller than the window (e.g. several outings on a small screen).
        self.side_scroller = ScrollableFrame(outer, background=self.palette.window, fit_width=True, autohide=True)
        self.side_scroller.grid(row=1, column=0, sticky="nsw", padx=(0, 16))
        side = self.side_scroller.inner
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

        def field(label: str, var: tk.StringVar, values: list[str], width: int = 26) -> ttk.Combobox:
            nonlocal r
            ttk.Label(side, text=label).grid(row=r, column=0, sticky="w", pady=3, padx=(0, 8))
            box = ttk.Combobox(side, textvariable=var, state="readonly", width=width, values=values)
            box.grid(row=r, column=1, columnspan=2, sticky="ew", pady=3)
            box.bind("<<ComboboxSelected>>", lambda e: self._schedule_recompute())
            r += 1
            return box

        section("Time in the sun")
        self.outing_list = OutingList(side, self._on_outing_picked)
        self.outing_list.grid(row=r, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        r += 1
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
        field("Shade", self.shade_var, [c.label for c in SHADE_CHOICES])
        field("Ground", self.surface_var, [c.label for c in SURFACE_CHOICES])
        self.spf_box = field("Sunscreen", self.spf_var, [l for l, _ in SPF_CHOICES])
        outing_buttons = ttk.Frame(side)
        outing_buttons.grid(row=r, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self.add_btn = ttk.Button(outing_buttons, text="+ Add an outing", command=self._add_outing)
        self.add_btn.pack(side="left", padx=(0, 6))
        self.remove_btn = ttk.Button(outing_buttons, text="Remove this one", command=self._remove_outing)
        r += 1

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
        field("Applied", self.application_var, [c.label for c in APPLICATION_CHOICES])
        self.sunscreen_hint = tk.StringVar()
        hint(self.sunscreen_hint)
        field("Skin exposed", self.body_var, [b.label for b in BODY_EXPOSURES])
        ttk.Label(side, text="Vitamin D goal").grid(row=r, column=0, sticky="w", pady=3)
        tgt = ttk.Frame(side)
        tgt.grid(row=r, column=1, columnspan=2, sticky="w", pady=3)
        # The arrows step through GOAL_STEPS_IU (1,000 to 10,000); any other value can be typed.
        tspin = ttk.Spinbox(tgt, textvariable=self.target_var, from_=100, to=20000, width=7)
        tspin.pack(side="left")
        tspin.bind("<<Increment>>", lambda e: self._step_goal(+1))
        tspin.bind("<<Decrement>>", lambda e: self._step_goal(-1))
        tspin.bind("<Return>", lambda e: self._schedule_recompute())
        tspin.bind("<FocusOut>", lambda e: self._schedule_recompute())
        ttk.Label(tgt, text="IU").pack(side="left", padx=(6, 0))
        r += 1
        self.goal_hint = tk.StringVar()
        hint(self.goal_hint)

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
        main.rowconfigure(1, weight=1)

        # The results (cards and note) sit in a block whose height may grow but doesn't shrink
        # back while the width stays the same. Otherwise every change that adds or removes a line
        # of text would resize the chart below, which redraws it a second time.
        results = ttk.Frame(main)
        results.grid(row=0, column=0, sticky="new")
        results.columnconfigure(0, weight=1)
        _keep_row_height(results, main, 0)
        cards = ttk.Frame(results)
        cards.grid(row=0, column=0, sticky="ew")
        self._cards_frame = cards
        self.cards: dict[str, ResultCard] = {}
        for key, title in (("dose", "UV dose"), ("burn", "Time to burn"), ("vitd", "Vitamin D"), ("uv", "Peak UV")):
            self.cards[key] = ResultCard(cards, title, self.palette)
        self._layout_cards(4)
        cards.bind("<Configure>", lambda e: e.width > 1 and self._layout_cards(  # 1: not laid out yet
            4 if e.width >= CARDS_ONE_ROW_MIN_WIDTH else 2))

        self.note_var = tk.StringVar()
        note = ttk.Label(results, textvariable=self.note_var, style="Muted.TLabel", font=f["caption"],
                         justify="left")
        note.grid(row=1, column=0, sticky="ew", pady=(6, 4))

        def wrap_note(event: tk.Event) -> None:
            width = max(event.width - 8, 200)
            if int(str(note.cget("wraplength")) or 0) != width:  # reconfiguring re-lays out the window
                note.configure(wraplength=width)

        main.bind("<Configure>", wrap_note, add="+")

        self.tabs = ttk.Notebook(main)
        self.tabs.grid(row=1, column=0, sticky="nsew")
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
        self._update_hints()
        self._refresh_outing_controls()
        # Lay the window out now, so the sidebar's width is settled before the first chart is
        # drawn. Otherwise the chart is drawn, then narrowed by the sidebar, then drawn again.
        self.root.update_idletasks()
        self.side_scroller.refit()

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
        self.side_scroller.set_background(self.palette.window)
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
        # What was on screen before a Refresh of the same day, to report what's new.
        before = self.day if force and self.day and self.day.site == site and self.day.day == d else None
        if not quiet:
            self.status_var.set(f"Loading {site.name}…")

        def ok(data: DayData) -> None:
            if rid != self._request_id:
                return  # a newer request superseded this one
            self.day = data
            self._on_day_loaded(before)

        def err(exc: Exception) -> None:
            if rid != self._request_id:
                return
            self._pending_start_now = False
            self.status_var.set(f"⚠ {str(exc).split(' (')[0]}")  # full reason goes on the chart
            if d == date.today():  # a dropped connection shouldn't stop the live updates: try again later
                self._schedule_live_refresh()
            if self.day is None or self.day.site != site or self.day.day != d:
                self.day = None
                self._show_message(f"Couldn't load UV data: {exc}\nCheck your connection, then press Refresh (F5).")

        if d > date.today():  # nothing is published ahead: build a clear-sky outlook from today
            self._submit(lambda: self.client.outlook(site, d, force=force), ok, err)
        else:
            self._submit(lambda: self.client.day(site, d, force=force), ok, err)

    def _schedule_live_refresh(self) -> None:
        self._later("refresh", AUTO_REFRESH_MS, lambda: self._load_day(quiet=True))

    def _on_day_loaded(self, before: DayData | None = None) -> None:
        """Show a newly loaded day. ``before`` is what was shown, when this load was a Refresh of that day."""
        day = self.day
        assert day is not None
        live = not day.is_complete and day.day == date.today()
        if live:  # readings are still coming in (or haven't started yet)
            self._schedule_live_refresh()
        if not day.has_data:
            self.status_var.set("")
            if day.outlook_from:
                self._show_message(f"ARPANSA has no clear-sky forecast for {day.site.name} today,\n"
                                   "so there's nothing to base an outlook on. Try another city.")
            elif day.day >= date.today():
                self._show_message(f"No UV data yet for {day.site.name}.\nTry another city.")
            else:
                self._show_message(f"No UV data for {day.site.name} on this date.\n"
                                   "Records start later at some sites. Try a more recent date or another city.")
            return
        latest = day.latest_measured
        if before is not None and latest:
            self.status_var.set(_refresh_summary(before, day))
        elif live and latest:
            self.status_var.set(f"Live · latest reading {latest.time:%H:%M}")
        elif day.outlook_from:
            self.status_var.set("Clear-sky outlook")
        elif day.has_measurements:
            self.status_var.set("Measured data")
        else:
            self.status_var.set("Clear-sky forecast only")

        if self._first_load:
            self._first_load = False
            if live and latest and latest.time.hour < 18 and len(self.outings) == 1:
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

    def _application(self) -> Choice:
        label = self.application_var.get()
        return next((c for c in APPLICATION_CHOICES if c.label == label), choice(APPLICATION_CHOICES,
                                                                                DEFAULT_APPLICATION))

    def _body(self) -> BodyExposure:
        return next((b for b in BODY_EXPOSURES if b.label == self.body_var.get()), BODY_EXPOSURES[1])

    def _target_iu(self) -> int:
        try:
            return max(100, min(int(float(self.target_var.get().replace(",", ""))), 20000))
        except ValueError:
            return 1000

    def _step_goal(self, direction: int) -> str:
        self.target_var.set(f"{next_goal(self._target_iu(), direction):.0f}")
        self._schedule_recompute()
        return "break"  # replaces the spinbox's own stepping

    def _sunscreen_on(self) -> bool:
        """False when the amount applied is "None": no outing gets its SPF."""
        return self._application().value > 0

    def _resolve(self, day: DayData) -> list[tuple[OutingSpec, Outing]]:
        """Place each outing on the shown day, inside the hours that have data, in time order.

        The selected outing's fields show the result, so a time outside the data
        (e.g. 05:00) visibly snaps to the first reading.
        """
        first, last = day.first_time, day.end_time
        assert first and last
        applied = self._application().value
        pairs: list[tuple[OutingSpec, Outing]] = []
        for spec in self.outings:
            start = max(datetime.combine(day.day, spec.start), first)
            end = min(datetime.combine(day.day, spec.end), last)
            if start >= last:
                start = last - timedelta(minutes=30)
            if end <= start:
                end = min(start + timedelta(hours=1), last)
            if spec is self.sel:
                spec.start, spec.end = start.time(), end.time()
                self.start_var.set(fmt_clock(start))
                self.end_var.set(fmt_clock(end))
            factor = exposure_factor(choice(SHADE_CHOICES, spec.shade).value,
                                     choice(SURFACE_CHOICES, spec.surface).value,
                                     effective_spf(spec.spf, applied))
            pairs.append((spec, Outing(start, end, factor)))
        pairs.sort(key=lambda pair: (pair[1].start, pair[1].end))
        self.outings = [spec for spec, _ in pairs]
        return pairs

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
        if self._on_chart_click_select(t):
            return
        st, et = parse_time(self.start_var.get()), parse_time(self.end_var.get())
        if st and et and t.time() < et:
            self._set_window(t.time(), et)
        else:
            length = (datetime.combine(t.date(), et) - datetime.combine(t.date(), st)) if st and et else None
            length = length if length and length > timedelta(0) else timedelta(hours=1)
            self._set_window(t.time(), (t + length).time() if (t + length).date() == t.date() else time(23, 59))

    # ================================================================== outings

    def _push_selected(self) -> None:
        """Show the selected outing in the sidebar fields."""
        o = self.sel
        self.start_var.set(fmt_clock(o.start))
        self.end_var.set(fmt_clock(o.end))
        self.shade_var.set(choice(SHADE_CHOICES, o.shade).label)
        self.surface_var.set(choice(SURFACE_CHOICES, o.surface).label)
        self.spf_var.set(next((label for label, v in SPF_CHOICES if v == o.spf), SPF_CHOICES[0][0]))

    def _pull_selected(self) -> None:
        """Copy the sidebar fields into the selected outing."""
        o = self.sel
        o.start = parse_time(self.start_var.get()) or o.start
        o.end = parse_time(self.end_var.get()) or o.end
        o.shade = next((c.key for c in SHADE_CHOICES if c.label == self.shade_var.get()), o.shade)
        o.surface = next((c.key for c in SURFACE_CHOICES if c.label == self.surface_var.get()), o.surface)
        o.spf = dict(SPF_CHOICES).get(self.spf_var.get(), o.spf)

    def _select_outing(self, spec: OutingSpec) -> None:
        if spec is self.sel:
            return
        self._pull_selected()
        self.sel = spec
        self._push_selected()
        self._recompute()

    def _on_outing_picked(self, index: int) -> None:
        if 0 <= index < len(self.outings):
            self._select_outing(self.outings[index])

    def _add_outing(self) -> None:
        if len(self.outings) >= MAX_OUTINGS:
            return
        self._pull_selected()
        start, end = self._free_slot()
        spec = self.sel.copy_conditions(start, end)  # same shade, ground and sunscreen as the selected one
        self.outings.append(spec)
        self.outings.sort(key=lambda o: (o.start, o.end))
        self.sel = spec
        self._push_selected()
        self._refresh_outing_controls()
        self._recompute()

    def _remove_outing(self) -> None:
        if len(self.outings) < 2:
            return
        i = self.outings.index(self.sel)
        self.outings.pop(i)
        self.sel = self.outings[min(i, len(self.outings) - 1)]
        self._push_selected()
        self._refresh_outing_controls()
        self._recompute()

    def _free_slot(self, length: timedelta = timedelta(minutes=30)) -> tuple[time, time]:
        """Times for a new outing: an hour after the last one, else before the first, else in the widest gap."""
        d = self.date_ctl.value
        if self.day and self.day.first_time and self.day.end_time:
            lo, hi = self.day.first_time, self.day.end_time
        else:
            lo, hi = datetime.combine(d, time(6)), datetime.combine(d, time(20))
        spans = sorted((max(datetime.combine(d, o.start), lo), min(datetime.combine(d, o.end), hi))
                       for o in self.outings)
        gap = timedelta(hours=1)
        after = _round_up_5(spans[-1][1] + gap)
        if after + length <= hi:
            return after.time(), (after + length).time()
        before = _round_down_5(spans[0][0] - gap - length)
        if before >= lo:
            return before.time(), (before + length).time()
        gaps = list(zip([lo] + [e for _, e in spans], [s for s, _ in spans] + [hi]))
        g0, g1 = max(gaps, key=lambda g: g[1] - g[0])
        start = _round_down_5(max(g0 + (g1 - g0 - length) / 2, g0))
        return start.time(), min(start + length, hi).time()

    def _refresh_outing_controls(self) -> None:
        many = len(self.outings) > 1
        if many and not self.outing_list.winfo_manager():
            self.outing_list.grid()
            self.remove_btn.pack(side="left")
        elif not many and self.outing_list.winfo_manager():
            self.outing_list.grid_remove()
            self.remove_btn.pack_forget()
        full = len(self.outings) >= MAX_OUTINGS
        if self.add_btn.instate(["disabled"]) != full:
            self.add_btn.state(["disabled"] if full else ["!disabled"])

    def _on_chart_click_select(self, t: datetime) -> bool:
        """A click inside another outing selects it. Returns True if it did."""
        for spec, o in self._resolved:
            if spec is not self.sel and o.start <= t < o.end:
                self._select_outing(spec)
                return True
        return False

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

    def _update_hints(self) -> None:
        self._update_med_hint()
        self._sync_spf_field()
        _set_if_changed(self.sunscreen_hint, "Most people apply half the tested amount or less."
                        if self._sunscreen_on() else "Sunscreen is off for every outing.")
        _set_if_changed(self.goal_hint,
                        f"Up to about {most_before_burning(self._body().fraction):,.0f} IU before you burn.")

    def _sync_spf_field(self) -> None:
        """While sunscreen is off, the Sunscreen field says so (and is locked); each outing keeps its SPF."""
        spf_labels = dict(SPF_CHOICES)
        if self._sunscreen_on():
            if self.spf_var.get() not in spf_labels:
                self.spf_var.set(next((label for label, v in SPF_CHOICES if v == self.sel.spf), SPF_CHOICES[0][0]))
            if self.spf_box.instate(["disabled"]):
                self.spf_box.state(["!disabled"])
        else:
            if self.spf_var.get() != SPF_OFF_LABEL:
                self.sel.spf = spf_labels.get(self.spf_var.get(), self.sel.spf)
                self.spf_var.set(SPF_OFF_LABEL)
            if not self.spf_box.instate(["disabled"]):
                self.spf_box.state(["disabled"])

    def _update_med_hint(self) -> None:
        st = self._skin()
        med = self._med()
        custom = abs(med - st.med_sed) > 1e-9
        text = f"Type {st.roman} default: {st.med_sed:g} SED (published range {st.med_range})."
        if custom:
            text = "Custom threshold. " + text
        _set_if_changed(self.med_hint, text)

    # ================================================================== calculations -> UI

    def _schedule_recompute(self) -> None:
        self._later("recompute", RECOMPUTE_DELAY_MS, self._recompute)

    def _recompute(self) -> None:
        self._update_hints()
        self._pull_selected()
        day = self.day
        if not day or not day.has_data:
            self._fill_outing_list(None, 0)
            return
        source = Source(self.source_var.get())
        series = build_series(day, source)
        pairs = self._resolve(day)
        self._resolved = pairs
        skin, med, body, target = self._skin(), self._med(), self._body(), self._target_iu()
        plan = plan_day(series, [o for _, o in pairs], med_sed=med, target_iu=target, body_fraction=body.fraction)
        selected = self.outings.index(self.sel)
        protection = sun_protection_times(series)
        clear = None
        if source is Source.BEST and day.has_forecast and plan.minutes > plan.forecast_minutes:
            clear_series = build_series(day, Source.CLEAR_SKY)
            clear = (clear_series, sum(window_dose(clear_series, o.start, o.end).sed for o in plan.outings))

        self._fill_outing_list(plan, selected)
        self._fill_cards(series, source, plan, protection, clear, skin, med, body, target)
        self._fill_note(day, source, plan)
        self._fill_chart(day, series, source, plan, selected, protection, clear, med)
        self._fill_table(series, plan)
        self._save_settings_soon()

    def _fill_outing_list(self, plan: DayPlan | None, selected: int) -> None:
        if plan is None:  # no data for the day: list the outings as set, without doses
            self.outings.sort(key=lambda o: (o.start, o.end))
            rows = [(f"{o.start:%H:%M}–{o.end:%H:%M}", o.conditions(self._sunscreen_on()), "–")
                    for o in self.outings]
            selected = self.outings.index(self.sel)
        else:
            rows = [(f"{o.start:%H:%M}–{o.end:%H:%M}", spec.conditions(self._sunscreen_on()), f"{dose:.2f} SED")
                    for (spec, o), dose in zip(self._resolved, plan.skin_doses)]
        self.outing_list.set_rows(rows, selected)
        self._refresh_outing_controls()

    def _adjustment_words(self) -> str:
        words = []
        if any(o.shade != "sun" for o in self.outings):
            words.append("shade")
        if any(o.surface != "ground" for o in self.outings):
            words.append("reflection")
        if self._sunscreen_on() and any(o.spf > 1 for o in self.outings):
            words.append("sunscreen")
        return ", ".join(words[:-1]) + (" and " if len(words) > 1 else "") + words[-1] if words else ""

    def _fill_cards(self, series, source, plan: DayPlan, protection: ProtectionTimes | None, clear, skin, med,
                    body, target) -> None:
        p = self.palette
        many = len(plan.outings) > 1
        first, last = plan.outings[0], plan.outings[-1]
        span = f"{len(plan.outings)} outings" if many else f"{first.start:%H:%M}–{first.end:%H:%M}"
        sun = fmt_duration(plan.sun_time)
        dose_span = f"{span}, {sun}" if many else span
        rating = rate_dose(plan.skin_sed, med)

        # ---- dose
        if abs(plan.skin_sed - plan.ambient_sed) > 0.005:
            line1 = f"≈{plan.skin_sed:.2f} SED on your skin after {self._adjustment_words()}"
        else:
            line1 = f"{plan.ambient_sed * 100:,.0f} J/m²" + (" in total" if many else f" over {sun}")
        extra = [rating.advice]
        if clear and clear[1] > 0.05:
            ratio = plan.ambient_sed / clear[1]
            if ratio < 0.95:
                extra.append(f"Cloud cut this by {1 - ratio:.0%} (clear sky: {clear[1]:.1f} SED).")
            elif ratio > 1.05:
                extra.append(f"{ratio - 1:.0%} above the clear-sky forecast ({clear[1]:.1f} SED).")
            else:
                extra.append("About the same as a clear-sky day.")
        if plan.forecast_minutes and source is Source.BEST and plan.forecast_minutes < plan.minutes:
            extra.append(f"{plan.forecast_minutes} min from the clear-sky forecast.")
        self.cards["dose"].show(
            f"{plan.ambient_sed:.2f}", "SED", line1,
            f"{rating.label} · {rating.fraction_of_med:.0%} of your burn threshold",
            " ".join(extra), color=status_color(p, rating.level), icon=LEVEL_ICON[rating.level],
            title=f"UV dose · {dose_span}",
        )

        # ---- burn
        who = f"Type {skin.roman}, {med:g} SED"
        notes = []
        if plan.relies_on_forecast:
            notes.append("Includes clear-sky forecast; cloud would make it later.")
        title = "Time to burn · time in the sun" if many else f"Time to burn · from {first.start:%H:%M}"
        b = plan.burn
        if b:
            inside = b.outing is not None
            where = f", in your {plan.outings[b.outing].start:%H:%M} outing" if many and inside else ""
            if inside:
                advice = ("Cover up or find shade by then." if many
                          else "Inside your window: cover up or find shade by then.")
            else:
                advice = f"Your {'last outing' if many else 'window'} ends {fmt_duration(b.at - last.end)} before that."
            if (self._sunscreen_on() and any(o.spf > 1 for o in self.outings)
                    and b.sun_time > timedelta(hours=REAPPLY_HOURS)):
                notes.append(f"Reapply sunscreen every {REAPPLY_HOURS} hours.")
            self.cards["burn"].show(
                fmt_duration(b.sun_time), "", f"Burn threshold at {b.at:%H:%M}{where} ({who})", advice,
                " ".join(notes), color=p.critical if inside else p.good,
                icon=LEVEL_ICON["very_high"] if inside else LEVEL_ICON["low"], title=title,
            )
        else:
            self.cards["burn"].show(
                "Not today", "", f"At most {plan.dose_available:.1f} SED on your skin ({who})",
                "Stays under your burn threshold for the rest of the day.", " ".join(notes),
                color=p.good, icon=LEVEL_ICON["low"], title=title,
            )

        # ---- vitamin D
        body_short = body.label.split(" (")[0].lower()
        capped = plan.skin_sed >= med * PLATEAU_MED
        line3 = (f"{'All outings' if many else 'Whole window'}: ≈{plan.vitd_iu:,.0f} IU"
                 + (" (production levels off)." if capped else "."))
        title = "Vitamin D · time in the sun" if many else f"Vitamin D · from {first.start:%H:%M}"
        v = plan.vitd
        if plan.vitd_dose_sed is None:
            self.cards["vitd"].show(
                "Not safely", "", f"{target:,} IU would take more than a burning dose on {body_short}.",
                f"The most before burning is ≈{most_before_burning(body.fraction):,.0f} IU. "
                "Expose more skin or lower the goal.", line3,
                color=p.warning, icon=LEVEL_ICON["moderate"], title=title,
            )
        elif v:
            where = f", in your {plan.outings[v.outing].start:%H:%M} outing" if many and v.outing is not None else ""
            margin = (f"Sweet spot: before your burn time ({fmt_duration(plan.burn.sun_time)})." if plan.burn
                      else "Sweet spot, with no burn risk today.")
            self.cards["vitd"].show(
                fmt_duration(v.sun_time), "", f"≈{target:,} IU by {v.at:%H:%M} with {body_short}{where}",
                margin, "After that, more sun mostly adds UV damage. " + line3,
                color=p.good, icon=LEVEL_ICON["low"], title=title,
            )
        else:
            possible = vitamin_d_iu(plan.dose_available, med, body.fraction)
            self.cards["vitd"].show(
                "Not today", "",
                f"Too little UV reaches your skin in these outings for {target:,} IU" if many
                else f"Too little UV reaches your skin after {first.start:%H:%M} for {target:,} IU",
                f"About {possible:,.0f} IU possible with {body_short}. "
                + ("Try longer or earlier outings." if many else "Try an earlier start."), line3,
                color=p.warning, icon=LEVEL_ICON["moderate"], title=title,
            )

        # ---- UV and sun protection times
        prot = (f"Sun protection {protection.start:%H:%M}–{protection.end:%H:%M}" if protection
                else "UV under 3 all day")
        title = "Peak UV · your outings" if many else f"Peak UV · {span}"
        pk = plan.peak
        if pk and pk.peak_time:
            cat = uv_category(pk.peak_uvi)
            self.cards["uv"].show(
                f"{pk.peak_uvi:.1f}", cat.name, f"at {pk.peak_time:%H:%M} · average {plan.mean_uvi:.1f}",
                prot, cat.advice, color=cat.color, icon="●", title=title,
            )
        else:
            self.cards["uv"].show("–", "", "No readings", prot, title=title)

    def _fill_note(self, day: DayData, source: Source, plan: DayPlan) -> None:
        latest = day.latest_measured
        if day.outlook_from:
            note = (f"Outlook: ARPANSA's clear-sky forecast for today ({day.outlook_from:%a %d %b}), "
                    "used as a stand-in for this day. Cloud would make real UV lower.")
        elif source is Source.CLEAR_SKY:
            note = "Showing ARPANSA's clear-sky forecast: the most UV a cloudless day would bring."
        elif not day.has_measurements:
            note = ("No measurements for this day, so ARPANSA's clear-sky forecast is used: "
                    "the most UV a cloudless day would bring.")
        elif not day.is_complete and latest and day.day == date.today():
            note = (f"Measured by ARPANSA until {latest.time:%H:%M}. After that the clear-sky forecast is used, "
                    f"so real UV may be lower if cloud comes over. New readings load every "
                    f"{AUTO_REFRESH_MS // 60_000} minutes.")
        elif plan.forecast_minutes:
            note = (f"{plan.forecast_minutes} minutes in {'your outings' if len(plan.outings) > 1 else 'this window'}"
                    " have no measurement, so ARPANSA's clear-sky forecast fills them.")
        elif plan.missing_minutes:
            note = f"{plan.missing_minutes} minutes have no data and count as zero."
        else:
            note = f"Measured by ARPANSA at {day.site.name}, one reading per minute."
        if plan.overlapped:
            note += " Some outings overlap; the shared minutes count once."
        self.note_var.set(note)

    def _fill_chart(self, day, series: list[UVPoint], source, plan: DayPlan, selected: int,
                    protection: ProtectionTimes | None, clear, med) -> None:
        latest = day.latest_measured
        live = not day.is_complete and latest is not None and day.day == date.today()
        curve, tail = plan_curves(series, plan.outings)
        clear_curve = plan_curves(clear[0], plan.outings)[0] if clear else ([], [])
        many = len(plan.outings) > 1
        first, last_end = plan.outings[0].start, plan.outings[-1].end
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
            if t > first:
                in_plan = t <= last_end
                dose = value_at(curve if in_plan else tail, t)
                if dose is not None:
                    since = f"since {first:%H:%M}" if not many else ("so far" if in_plan else "if you stayed out")
                    parts.append(f"{dose:.2f} SED on skin {since}")
            return "  ·  ".join(parts)

        title = f"{day.site.name} · {day.day:%a %d %b %Y}" + (" · clear-sky outlook" if day.outlook_from else "")
        self.chart.render(ChartView(
            title=title,
            day_start=day.first_time,
            day_end=day.end_time,
            times=[s.time for s in day.samples],
            measured=[s.measured for s in day.samples],
            forecast=[s.forecast for s in day.samples],
            show_measured=source is Source.BEST and day.has_measurements,
            latest_reading=latest.time if live else None,
            outings=[(o.start, o.end) for _, o in self._resolved],
            selected=selected,
            protection=(protection.start, protection.end) if protection else None,
            curve=curve,
            tail=tail,
            clear_curve=clear_curve,
            med_sed=med,
            vitd_sed=plan.vitd_dose_sed,
            burn_at=plan.burn.at if plan.burn else None,
            vitd_at=plan.vitd.at if plan.vitd else None,
            describe=describe,
            **({"hint": "Drag to set times · click an outing to edit it"} if many else {}),
        ))

    def _fill_table(self, series, plan: DayPlan) -> None:
        self.table.delete(*self.table.get_children())
        rows = plan_hours(series, plan.outings)
        self._hour_rows = rows
        for row in rows:
            notes = []
            if row.forecast_minutes:
                notes.append(f"clear-sky forecast {row.forecast_minutes} min")
            if row.missing_minutes:
                notes.append(f"no data {row.missing_minutes} min")
            self.table.insert("", "end", values=(
                f"{row.start:%H:%M}–{row.end:%H:%M}", f"{row.mean_uvi:.1f}", f"{row.peak_uvi:.1f}",
                f"{row.sed:.2f}", f"{row.cumulative_sed:.2f}", f"{row.skin_cumulative_sed:.2f}",
                "   " + (", ".join(notes) or "measured"),
            ))
        total = plan.ambient_sed
        first, last = plan.outings[0], plan.outings[-1]
        if len(plan.outings) > 1:
            what = (f"{first.start:%d/%m/%Y}, {len(plan.outings)} outings "
                    f"({fmt_duration(plan.sun_time)} in the sun)")
            edges = "Rows are clock hours within each outing."
        else:
            what = f"{first.start:%d/%m/%Y} {first.start:%H:%M}–{last.end:%H:%M}"
            edges = "Partial hours at the edges of the window."
        self.table_caption.set(f"{self.site.name}, {what}: {total:.2f} SED in total ({total * 100:,.0f} J/m²). "
                               f"{edges}")

    # ================================================================== misc actions

    def _copy_csv(self) -> None:
        if not self._hour_rows:
            self.status_var.set("Nothing to copy yet")
            return
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(["site", "date", "outing", "from", "to", "minutes", "mean_uvi", "peak_uvi", "dose_sed",
                    "running_total_sed", "on_skin_running_total_sed", "forecast_minutes", "missing_minutes"])
        for r in self._hour_rows:
            w.writerow([self.site.name, r.start.date().isoformat(), r.outing + 1, f"{r.start:%H:%M}",
                        f"{r.end:%H:%M}", r.minutes, f"{r.mean_uvi:.2f}", f"{r.peak_uvi:.2f}", f"{r.sed:.3f}",
                        f"{r.cumulative_sed:.3f}", f"{r.skin_cumulative_sed:.3f}", r.forecast_minutes,
                        r.missing_minutes])
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
        s.body_key = self._body().key
        s.target_iu = self._target_iu()
        s.application = self._application().key
        s.source = self.source_var.get()
        s.dark = self.dark_var.get()
        self._pull_selected()
        s.outings = [o.to_dict() for o in self.outings]
        s.save()

    def _on_close(self) -> None:
        self._save_settings()
        self.root.destroy()


def _set_if_changed(var: tk.StringVar, value: str) -> None:
    """Set a label's text only when it differs: setting it repaints the label even if it's the same."""
    if var.get() != value:
        var.set(value)


def _keep_row_height(content: tk.Widget, parent: tk.Misc, row: int) -> None:
    """Let ``parent``'s grid ``row`` grow with ``content`` but not shrink, until the width changes.

    ``content`` must be gridded with ``sticky`` excluding "s", so its own height
    stays its natural height and shows up in its <Configure> events.
    """
    seen = {"width": -1, "height": 0}

    def on_configure(event: tk.Event) -> None:
        if event.width != seen["width"]:  # new width: text rewraps, so start again from scratch
            seen["width"], seen["height"] = event.width, 0
            parent.rowconfigure(row, minsize=0)
        elif event.height > seen["height"]:
            seen["height"] = event.height
            parent.rowconfigure(row, minsize=event.height)

    content.bind("<Configure>", on_configure, add="+")


def _refresh_summary(before: DayData, after: DayData) -> str:
    """Status after a Refresh: how many new readings came in, or that there were none."""
    prev = before.latest_measured
    latest = after.latest_measured
    assert latest is not None
    new = sum(1 for s in after.samples if s.measured is not None and (prev is None or s.time > prev.time))
    if not new:
        return f"Up to date · latest reading {latest.time:%H:%M}"
    return f"{new} new reading{'' if new == 1 else 's'} · latest {latest.time:%H:%M}"


def _round_up_5(t: datetime) -> datetime:
    t = t.replace(second=0, microsecond=0)
    return t + timedelta(minutes=-t.minute % 5)


def _round_down_5(t: datetime) -> datetime:
    t = t.replace(second=0, microsecond=0)
    return t - timedelta(minutes=t.minute % 5)


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
This is the time in the sun from your start until the dose on your skin reaches your burn threshold, following the real UV curve rather than assuming constant UV. If that doesn't happen within your window, the app carries on as if you stayed out, to show how much longer you could stay.

## Several outings
Sunburn and vitamin D depend on the total dose over the day, so "+ Add an outing" lets you plan a day in pieces, such as a morning walk and a lunch break. The doses add up. The burn and vitamin D times then count only the time spent outside, and say which outing they fall in. Each outing has its own shade, ground and sunscreen. Pick one in the list, or click it on the chart, to edit it. If outings overlap, the shared minutes count once.

## Shade, ground and sunscreen
ARPANSA measures UV on a flat, open sensor. Each outing's conditions scale that dose:
    dose on skin = ambient dose × (shade + reflection) ÷ effective SPF
Shade blocks the direct sun but not UV scattered from the open sky, which is roughly half the total in summer around midday. Patchy shade counts as 0.75 and good shade as 0.5 (the cautious end; good shade can cut UV by up to 75%).
Reflection from the ground adds UV, mostly to your face, neck and the underside of your arms. Following the WHO's typical figures, water adds 10%, beach sand 15% and fresh snow 80%. Grass, soil and paving reflect under 10% and count as zero. Shade overhead doesn't stop reflection from the sunlit ground around you, so it is added rather than reduced.
SPF ratings assume a thick layer (2 mg/cm², about a teaspoon per limb). Most people apply half to a quarter of that. The app takes protection to drop in proportion: SPF 30 applied at half thickness acts like SPF 15.5. Some studies find it drops faster, so if you burn easily treat these as best cases. Sunscreen also wears off with sweat, swimming and towelling, so reapply every 2 hours.
Setting "Applied" to None turns sunscreen off for every outing, which is a quick way to see what going without would mean. Each outing keeps its SPF for when you turn it back on.
These are rough, typical figures, not measurements of your spot.

## Sun protection times
Cancer Council and ARPANSA recommend sun protection whenever the UV Index is 3 or above. The chart marks that part of the day along the bottom of the UV panel, and the Peak UV card gives the times. They come from the UV data shown, so on a cloudy day they reflect what was measured.

## Tomorrow's outlook
ARPANSA publishes nothing ahead of time. Press ▶ past today to get a clear-sky outlook for tomorrow, made from today's clear-sky forecast. A clear-sky curve barely changes from one day to the next. The outlook allows for daylight saving starting or ending. It shows a cloudless day, so it is a cautious guide for sunburn and an optimistic one for vitamin D.

## Vitamin D
This uses Holick's rule of thumb, as adopted by Webb & Engelsen (2006): exposing about a quarter of the body (face, arms and hands) to a quarter of your burn threshold makes roughly 1000 IU. In general:
    IU ≈ 16,000 × (fraction of skin exposed) × (dose / burn threshold), capped at 1 burn-threshold dose.
Vitamin D production levels off with continued exposure, so short, regular exposure beats long sessions. The "sweet spot" is the time needed to reach your goal. Beyond it, extra sun mostly adds UV damage.
Caveats: the model treats erythemal UV as a stand-in for vitamin-D-producing UV. That overstates production when the sun is low (early, late and in winter). It ignores age, body size and diet.
Choosing a goal: the default of 1,000 IU is Webb & Engelsen's "standard vitamin D dose" for sun exposure. Official intake guidelines are lower: 200–600 IU a day in Australia and New Zealand, rising with age, and 600–800 IU in the US. The Endocrine Society's 2011 guideline suggested 1,500–2,000 IU for adults to keep blood levels up, but its 2024 update no longer advises more than the official amounts for healthy adults under 75. The goal's arrows step from 1,000 to 10,000 IU, and you can type any other value.
Higher goals need more bare skin rather than more time. The most a day can make before you burn is about 16,000 IU × the fraction of skin exposed: about 4,000 IU with face, arms and hands, and about 13,600 IU in swimwear. The skin can't make too much vitamin D, because production levels off, but UV damage keeps adding up. Vitamin D also lasts for weeks in the body, so a good dose on some days covers days with less.

## Measured vs clear-sky forecast
"Measured" uses ARPANSA's readings, and for any minute without one (for example later today) it uses ARPANSA's clear-sky forecast. While today's readings are coming in, the app loads new ones every 2 minutes. Press Refresh (F5) to load them straight away; the status at the top then says how many came in. "Clear-sky forecast" shows the most UV a cloudless day would bring. That is a cautious figure for sunburn and an optimistic one for vitamin D.

## Sunburn
Sunburn can take 16–24 hours to show fully.
"""


def run(client: ArpansaClient | None = None, settings: Settings | None = None) -> None:
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    App(root, client or ArpansaClient(), settings or Settings.load())
    root.mainloop()
