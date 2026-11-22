"""Small reusable widgets: time and date inputs, result cards, a scrollable frame."""

from __future__ import annotations

import re
import tkinter as tk
from datetime import date, datetime, time, timedelta
from tkinter import ttk
from typing import Callable

from uv_dose.gui.theme import Palette, fonts

# ------------------------------------------------------------------ parsing helpers

_TIME_RE = re.compile(r"^\s*(\d{1,2})(?:[:.h ]?(\d{2}))?\s*(am|pm)?\s*$", re.IGNORECASE)


def parse_time(text: str) -> time | None:
    """Accepts '14:30', '1430', '9', '2:30 pm', '14.30'."""
    m = _TIME_RE.match(text or "")
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    suffix = (m.group(3) or "").lower()
    if suffix:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if suffix == "pm" else 0)
    if hour == 24 and minute == 0:
        return time(23, 59)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return time(hour, minute)


def parse_date(text: str) -> date | None:
    """Accepts '21/11/2026' (Australian order), '21-11-2026' or ISO '2026-11-21'."""
    text = (text or "").strip()
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def fmt_date(d: date) -> str:
    return d.strftime("%d/%m/%Y")


def fmt_clock(t: datetime | time) -> str:
    return t.strftime("%H:%M")


def fmt_duration(td: timedelta) -> str:
    minutes = max(0, round(td.total_seconds() / 60))
    if minutes < 60:
        return f"{minutes} min"
    h, m = divmod(minutes, 60)
    return f"{h} h {m:02d} min" if m else f"{h} h"


# ------------------------------------------------------------------ time spinbox


class TimeSpinbox(ttk.Spinbox):
    """HH:MM entry whose arrows/wheel step in 5-minute increments (snapping to the grid)."""

    STEP = 5

    def __init__(self, master: tk.Misc, variable: tk.StringVar, on_commit: Callable[[], None], **kw) -> None:
        super().__init__(master, textvariable=variable, width=7, from_=0, to=1, **kw)
        self._var = variable
        self._on_commit = on_commit
        self.bind("<<Increment>>", lambda e: self._step(+1))
        self.bind("<<Decrement>>", lambda e: self._step(-1))
        self.bind("<Return>", lambda e: self.commit())
        self.bind("<KP_Enter>", lambda e: self.commit())
        self.bind("<FocusOut>", lambda e: self.commit())

    def _step(self, direction: int) -> str:
        t = parse_time(self._var.get()) or time(12, 0)
        minutes = t.hour * 60 + t.minute
        if direction > 0:
            minutes = (minutes // self.STEP + 1) * self.STEP
        else:
            minutes = ((minutes - 1) // self.STEP) * self.STEP
        minutes = max(0, min(minutes, 23 * 60 + 55))
        self._var.set(f"{minutes // 60:02d}:{minutes % 60:02d}")
        self._on_commit()
        return "break"

    def commit(self) -> None:
        t = parse_time(self._var.get())
        if t is not None:
            self._var.set(fmt_clock(t))
        self._on_commit()


# ------------------------------------------------------------------ date control


class DateControl(ttk.Frame):
    def __init__(self, master: tk.Misc, on_change: Callable[[date], None], max_days_ahead: int = 0) -> None:
        super().__init__(master)
        self._on_change = on_change
        self._ahead = timedelta(days=max_days_ahead)
        self._date = date.today()
        self.var = tk.StringVar(value=fmt_date(self._date))
        ttk.Button(self, text="◀", width=3, command=lambda: self.shift(-1)).grid(row=0, column=0)
        self.entry = ttk.Entry(self, textvariable=self.var, width=11, justify="center")
        self.entry.grid(row=0, column=1, padx=4)
        self.next_btn = ttk.Button(self, text="▶", width=3, command=lambda: self.shift(+1))
        self.next_btn.grid(row=0, column=2)
        ttk.Button(self, text="Today", command=self.today).grid(row=0, column=3, padx=(6, 0))
        self.weekday = ttk.Label(self, style="Muted.TLabel", width=10)
        self.weekday.grid(row=0, column=4, padx=(8, 0))
        self.entry.bind("<Return>", lambda e: self._commit_text())
        self.entry.bind("<FocusOut>", lambda e: self._commit_text())
        self._refresh()

    @property
    def value(self) -> date:
        return self._date

    def set(self, d: date, notify: bool = True) -> None:
        d = min(d, date.today() + self._ahead)
        changed = d != self._date
        self._date = d
        self._refresh()
        if changed and notify:
            self._on_change(d)

    def shift(self, days: int) -> None:
        self.set(self._date + timedelta(days=days))

    def today(self) -> None:
        self.set(date.today())

    def _commit_text(self) -> None:
        d = parse_date(self.var.get())
        if d is None:
            self._refresh()  # revert to the last good value
            return
        self.set(d)

    def _refresh(self) -> None:
        self.var.set(fmt_date(self._date))
        self.weekday.configure(text=self._date.strftime("%A"))
        self.next_btn.state(["disabled"] if self._date >= date.today() + self._ahead else ["!disabled"])


# ------------------------------------------------------------------ result card


class ResultCard(tk.Frame):
    """A tile with a coloured stripe, a title, a big value and up to three detail lines."""

    def __init__(self, master: tk.Misc, title: str, palette: Palette) -> None:
        super().__init__(master, highlightthickness=1, bd=0)
        f = fonts(self)
        self.stripe = tk.Frame(self, width=5)
        self.stripe.pack(side="left", fill="y")
        body = tk.Frame(self, padx=12, pady=8)
        body.pack(side="left", fill="both", expand=True)
        self._body = body
        self.title = tk.Label(body, text=title, font=f["caption"], anchor="w")
        self.title.pack(fill="x")
        row = tk.Frame(body)
        row.pack(fill="x")
        self._row = row
        self.value = tk.Label(row, text="–", font=f["value"], anchor="w")
        self.value.pack(side="left")
        self.unit = tk.Label(row, text="", font=f["strong"], anchor="sw")
        self.unit.pack(side="left", padx=(4, 0), pady=(10, 0))
        self.line1 = tk.Label(body, font=f["body"], anchor="w", justify="left")
        self.line1.pack(fill="x")
        badge = tk.Frame(body)
        badge.pack(fill="x", pady=(2, 0))
        self._badge = badge
        self.icon = tk.Label(badge, font=f["icon"], anchor="w")
        self.icon.pack(side="left")
        self.line2 = tk.Label(badge, font=f["strong"], anchor="w", justify="left")
        self.line2.pack(side="left", fill="x")
        self.line3 = tk.Label(body, font=f["caption"], anchor="w", justify="left")
        self.line3.pack(fill="x")
        self._compact = False  # hides the secondary detail line, to leave room for the chart
        self._stripe_color = palette.border
        self._icon_color = palette.ink
        self.apply_palette(palette)
        self.bind("<Configure>", self._rewrap)

    def _rewrap(self, event: tk.Event | None = None) -> None:
        width = max((event.width if event else self.winfo_width()) - 36, 120)
        # Reconfiguring a label re-lays out the window even when the value is the same, and this
        # runs on every resize of the card (including height changes), so skip unchanged values.
        for label, wrap in ((self.title, width), (self.line1, width), (self.line3, width),
                            (self.line2, max(width - self.icon.winfo_reqwidth(), 100))):
            if int(str(label.cget("wraplength")) or 0) != wrap:
                label.configure(wraplength=wrap)

    def apply_palette(self, p: Palette) -> None:
        self._palette = p
        bg = p.surface
        self.configure(background=bg, highlightbackground=p.border, highlightcolor=p.border)
        for w in (self._body, self._row, self._badge):
            w.configure(background=bg)
        self.title.configure(background=bg, foreground=p.ink2)
        self.value.configure(background=bg, foreground=p.ink)
        self.unit.configure(background=bg, foreground=p.ink2)
        self.line1.configure(background=bg, foreground=p.ink2)
        self.line2.configure(background=bg, foreground=p.ink)
        self.line3.configure(background=bg, foreground=p.ink2)
        self.icon.configure(background=bg, foreground=self._icon_color)
        self.stripe.configure(background=self._stripe_color)

    def show(
        self,
        value: str,
        unit: str = "",
        line1: str = "",
        line2: str = "",
        line3: str = "",
        *,
        color: str | None = None,
        icon: str = "",
        title: str | None = None,
    ) -> None:
        if title is not None:
            self.title.configure(text=title)
        self.value.configure(text=value)
        self.unit.configure(text=unit)
        self.line1.configure(text=line1)
        self.line2.configure(text=line2)
        self.line3.configure(text=line3)
        self._pack_line3()
        self._stripe_color = color or self._palette.border
        self._icon_color = color or self._palette.ink
        self.icon.configure(text=(icon + " ") if icon else "")
        self.apply_palette(self._palette)
        if self.winfo_width() > 1:
            self._rewrap()

    def set_compact(self, compact: bool) -> None:
        self._compact = compact
        self._pack_line3()

    def _pack_line3(self) -> None:
        if self._compact or not self.line3.cget("text"):
            self.line3.pack_forget()
        else:
            self.line3.pack(fill="x")


# ------------------------------------------------------------------ scrollable frame


class ScrollableFrame(ttk.Frame):
    """A vertically scrolling container; put children in ``.inner``.

    ``fit_width`` makes the frame as wide as its contents (for a sidebar), and
    ``autohide`` shows the scrollbar only when the contents are taller than the frame.
    """

    def __init__(self, master: tk.Misc, background: str, *, fit_width: bool = False, autohide: bool = False,
                 **kw) -> None:
        super().__init__(master, **kw)
        self._fit_width = fit_width
        self._autohide = autohide
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0, background=background)
        self.vbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vbar.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.vbar.grid(row=0, column=1, sticky="ns", padx=(4, 0) if fit_width else 0)
        if autohide:
            self.vbar.grid_remove()  # until the contents are known not to fit; usually they do
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.inner = ttk.Frame(self.canvas)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        # Last values applied. Reconfiguring the canvas or re-gridding the scrollbar re-lays out
        # the window even when nothing changes, so each is only done when its value changes.
        self._applied: dict[str, object] = {}
        self.inner.bind("<Configure>", self._on_inner)
        self.canvas.bind("<Configure>", self._on_canvas)
        # Only capture the mouse wheel while the pointer is over this frame.
        self.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", self._on_wheel))
        self.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))

    def set_background(self, color: str) -> None:
        self.canvas.configure(background=color)

    def _changed(self, key: str, value: object) -> bool:
        if self._applied.get(key) == value:
            return False
        self._applied[key] = value
        return True

    def refit(self) -> None:
        """Apply the contents' current size now (normally done when they report a resize)."""
        self._on_inner()

    def _on_inner(self, event: tk.Event | None = None) -> None:
        region = self.canvas.bbox("all")
        if self._changed("region", region):
            self.canvas.configure(scrollregion=region)
        if self._fit_width and self._changed("width", self.inner.winfo_reqwidth()):
            self.canvas.configure(width=self.inner.winfo_reqwidth())
        self._update_bar()

    def _on_canvas(self, event: tk.Event) -> None:
        if self._changed("inner_width", event.width):
            self.canvas.itemconfigure(self._win, width=event.width)
        self._update_bar()

    def _fits(self) -> bool:
        return self.inner.winfo_reqheight() <= self.canvas.winfo_height()

    def _update_bar(self) -> None:
        if not self._autohide or self.canvas.winfo_height() < 2:  # not laid out yet
            return
        fits = self._fits()
        if not self._changed("fits", fits):
            return
        if fits:
            self.vbar.grid_remove()
            self.canvas.yview_moveto(0)
        else:
            self.vbar.grid()

    def _on_wheel(self, event: tk.Event) -> None:
        if self._autohide and self._fits():
            return
        if isinstance(event.widget, (ttk.Spinbox, ttk.Combobox)):
            return  # those use the wheel to change their value
        self.canvas.yview_scroll(int(-event.delta / 120), "units")
