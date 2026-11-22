"""The day's outings as the user sets them up, and the sidebar list that shows them."""

from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass
from datetime import time
from tkinter import ttk
from typing import Any, Callable

from uv_dose.exposure import DEFAULT_SHADE, DEFAULT_SURFACE, SHADE_CHOICES, SURFACE_CHOICES, choice
from uv_dose.gui.widgets import fmt_clock, parse_time

MAX_OUTINGS = 6
SPF_VALUES = (1, 15, 30, 50)


@dataclass(eq=False)  # compared by identity: two outings can have the same times
class OutingSpec:
    """One outing as times of day, so the plan carries over to whichever date is shown."""

    start: time
    end: time
    shade: str = DEFAULT_SHADE
    surface: str = DEFAULT_SURFACE
    spf: int = 1

    @classmethod
    def from_dict(cls, raw: Any) -> "OutingSpec | None":
        if not isinstance(raw, dict):
            return None
        start, end = parse_time(str(raw.get("start", ""))), parse_time(str(raw.get("end", "")))
        if start is None or end is None:
            return None
        spf = raw.get("spf", 1)
        return cls(
            start=start,
            end=end,
            shade=choice(SHADE_CHOICES, str(raw.get("shade", DEFAULT_SHADE))).key,
            surface=choice(SURFACE_CHOICES, str(raw.get("surface", DEFAULT_SURFACE))).key,
            spf=spf if spf in SPF_VALUES else 1,
        )

    def to_dict(self) -> dict[str, Any]:
        return {"start": fmt_clock(self.start), "end": fmt_clock(self.end), "shade": self.shade,
                "surface": self.surface, "spf": self.spf}

    def copy_conditions(self, start: time, end: time) -> "OutingSpec":
        return OutingSpec(start, end, self.shade, self.surface, self.spf)

    def conditions(self, sunscreen: bool = True) -> str:
        """Short summary such as 'good shade · sand · SPF 30'. ``sunscreen=False`` leaves out the SPF."""
        parts = [choice(SHADE_CHOICES, self.shade).short]
        surface = choice(SURFACE_CHOICES, self.surface).short
        if surface:
            parts.append(surface)
        if sunscreen and self.spf > 1:
            parts.append(f"SPF {self.spf}" + ("+" if self.spf >= 50 else ""))
        return " · ".join(parts)


class OutingList(ttk.Frame):
    """A compact list of outings (time, conditions, dose on skin); picking a row selects that outing."""

    def __init__(self, master: tk.Misc, on_select: Callable[[int], None]) -> None:
        super().__init__(master)
        self._on_select = on_select
        self.tree = ttk.Treeview(self, columns=("when", "where", "dose"), show="", selectmode="browse", height=2)
        for col, width, anchor, stretch in (("when", 92, "w", False), ("where", 120, "w", True),
                                            ("dose", 66, "e", False)):
            self.tree.column(col, width=width, minwidth=40, anchor=anchor, stretch=stretch)
        self.tree.pack(fill="x")
        self.tree.bind("<<TreeviewSelect>>", self._selected)

    def set_rows(self, rows: list[tuple[str, str, str]], selected: int) -> None:
        items = self.tree.get_children()
        if len(items) != len(rows):
            self.tree.delete(*items)
            items = [self.tree.insert("", "end", iid=str(i), values=row) for i, row in enumerate(rows)]
            self.tree.configure(height=max(len(rows), 1))
        else:
            for iid, row in zip(items, rows):
                if tuple(self.tree.item(iid, "values")) != row:
                    self.tree.item(iid, values=row)
        if items and self.tree.selection() != (str(selected),):
            self.tree.selection_set(str(selected))
            self.tree.see(str(selected))

    def _selected(self, event: tk.Event) -> None:
        sel = self.tree.selection()
        if sel:
            self._on_select(int(sel[0]))
