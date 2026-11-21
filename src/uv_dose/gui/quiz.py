"""Fitzpatrick skin phototype questionnaire (ARPANSA version) as a modal dialog."""

from __future__ import annotations

import textwrap
import tkinter as tk
from tkinter import ttk

from uv_dose.gui.theme import Palette, fonts
from uv_dose.gui.widgets import ScrollableFrame
from uv_dose.skin import MAX_SCORE, QUESTIONS, SECTIONS, SkinType, score_answers, skin_type_from_score

# Section header colours from ARPANSA's phototype sheet.
SECTION_COLORS = {SECTIONS[0]: "#5b2d8c", SECTIONS[1]: "#24873f", SECTIONS[2]: "#e8743b"}


class FitzpatrickDialog(tk.Toplevel):
    """Returns ``(skin_type, answers)`` in ``self.result`` when the user confirms, else None."""

    def __init__(self, master: tk.Misc, palette: Palette, answers: dict[str, int] | None = None) -> None:
        super().__init__(master)
        self.title("Find your skin type (Fitzpatrick scale)")
        self.configure(background=palette.window)
        self.transient(master)
        self.resizable(True, True)
        self.result: tuple[SkinType, dict[str, int]] | None = None
        self.palette = palette
        f = fonts(self)

        self.vars: dict[str, tk.IntVar] = {}
        answers = answers or {}

        outer = ttk.Frame(self, padding=(16, 14, 16, 12))
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)

        ttk.Label(
            outer,
            text=(
                "Answer the ten questions from ARPANSA's Fitzpatrick skin phototype sheet. "
                "Your score gives your skin type and a typical burn threshold for the dose calculator."
            ),
            wraplength=820,
            justify="left",
        ).grid(row=0, column=0, sticky="ew", pady=(0, 10))

        scroller = ScrollableFrame(outer, background=palette.window)
        scroller.grid(row=1, column=0, sticky="nsew")
        grid = scroller.inner
        grid.columnconfigure(0, weight=2, minsize=230)
        for c in range(1, 6):
            grid.columnconfigure(c, weight=1, uniform="opt")

        row = 0
        for section in SECTIONS:
            head = tk.Frame(grid, background=SECTION_COLORS[section])
            head.grid(row=row, column=0, columnspan=6, sticky="ew", pady=(10 if row else 0, 4))
            tk.Label(head, text=section, font=f["strong"], fg="#ffffff", bg=SECTION_COLORS[section],
                     padx=8, pady=3).pack(side="left")
            tk.Label(head, text="score 0 → 4", font=f["caption"], fg="#ffffff", bg=SECTION_COLORS[section],
                     padx=8).pack(side="right")
            row += 1
            for q in (q for q in QUESTIONS if q.section == section):
                var = tk.IntVar(value=answers.get(q.key, -1))
                self.vars[q.key] = var
                ttk.Label(grid, text=q.text, wraplength=230, justify="left").grid(
                    row=row, column=0, sticky="w", padx=(4, 8), pady=5
                )
                for score, option in enumerate(q.options):
                    ttk.Radiobutton(
                        grid,
                        text=textwrap.fill(option, 18),
                        value=score,
                        variable=var,
                        command=self._update,
                    ).grid(row=row, column=score + 1, sticky="w", padx=2, pady=5)
                row += 1
                ttk.Separator(grid).grid(row=row, column=0, columnspan=6, sticky="ew")
                row += 1

        # ------------------------------------------------ result + buttons
        bottom = ttk.Frame(outer)
        bottom.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        bottom.columnconfigure(1, weight=1)
        self.swatch = tk.Canvas(bottom, width=44, height=44, highlightthickness=1,
                                highlightbackground=palette.border, background=palette.window)
        self.swatch.grid(row=0, column=0, rowspan=2, padx=(0, 12))
        self.result_title = ttk.Label(bottom, font=f["subtitle"])
        self.result_title.grid(row=0, column=1, sticky="w")
        self.result_detail = ttk.Label(bottom, style="Muted.TLabel", wraplength=600, justify="left")
        self.result_detail.grid(row=1, column=1, sticky="w")
        buttons = ttk.Frame(bottom)
        buttons.grid(row=0, column=2, rowspan=2, sticky="e")
        ttk.Button(buttons, text="Clear", command=self._clear).pack(side="left", padx=(0, 6))
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="left", padx=(0, 6))
        self.use_btn = ttk.Button(buttons, text="Use this skin type", style="Accent.TButton", command=self._accept)
        self.use_btn.pack(side="left")

        self.bind("<Escape>", lambda e: self.destroy())
        self._update()
        self._place(master)
        self.grab_set()
        self.focus_set()

    def _place(self, master: tk.Misc) -> None:
        self.update_idletasks()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w, h = min(1040, sw - 80), min(860, sh - 120)
        x = master.winfo_rootx() + max((master.winfo_width() - w) // 2, 0)
        y = master.winfo_rooty() + max((master.winfo_height() - h) // 2, 0)
        self.geometry(f"{w}x{h}+{max(x, 0)}+{max(y, 0)}")
        self.minsize(760, 480)

    def _answers(self) -> dict[str, int]:
        return {k: v.get() for k, v in self.vars.items() if v.get() >= 0}

    def _update(self) -> None:
        answers = self._answers()
        answered = len(answers)
        partial = sum(answers.values())
        self.swatch.delete("all")
        if answered < len(QUESTIONS):
            self.result_title.configure(text=f"{answered} of {len(QUESTIONS)} answered")
            self.result_detail.configure(
                text=f"Running score {partial} / {MAX_SCORE}. Answer every question to see your skin type."
            )
            self.use_btn.state(["disabled"])
            return
        score = score_answers(answers)
        st = skin_type_from_score(score)
        self.swatch.create_rectangle(0, 0, 46, 46, fill=st.color, outline="")
        self.result_title.configure(text=f"Type {st.roman}: {st.title}   (score {score} / {MAX_SCORE})")
        self.result_detail.configure(
            text=(
                f"{st.description} Example: {st.example}. "
                f"Typical burn threshold used here: {st.med_sed:g} SED (published range {st.med_range})."
            )
        )
        self.use_btn.state(["!disabled"])

    def _clear(self) -> None:
        for v in self.vars.values():
            v.set(-1)
        self._update()

    def _accept(self) -> None:
        answers = self._answers()
        if len(answers) < len(QUESTIONS):
            return
        self.result = (skin_type_from_score(score_answers(answers)), answers)
        self.destroy()
