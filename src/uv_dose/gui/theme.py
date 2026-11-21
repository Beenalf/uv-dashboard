"""Colours and fonts shared by the window and the chart (light and dark)."""

from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass
from tkinter import font as tkfont
from tkinter import ttk


@dataclass(frozen=True)
class Palette:
    name: str
    window: str  # matches the ttk theme background
    surface: str  # chart + card surface
    border: str
    ink: str
    ink2: str
    muted: str
    grid: str
    axis: str
    series: str  # primary data series (measured UV, dose on skin)
    forecast: str
    band_alpha: float
    select_alpha: float
    # status colours (fixed): good / warning / serious / critical
    good: str = "#0ca30c"
    warning: str = "#fab219"
    serious: str = "#ec835a"
    critical: str = "#d03b3b"
    good_text: str = "#006300"


LIGHT = Palette(
    name="light", window="#fafafa", surface="#ffffff", border="#e1e0d9",
    ink="#0b0b0b", ink2="#52514e", muted="#898781", grid="#e1e0d9", axis="#c3c2b7",
    series="#2a78d6", forecast="#898781", band_alpha=0.10, select_alpha=0.10,
)
DARK = Palette(
    name="dark", window="#1c1c1c", surface="#232322", border="#383835",
    ink="#f0efec", ink2="#c3c2b7", muted="#898781", grid="#2c2c2a", axis="#383835",
    series="#3987e5", forecast="#a3a29b", band_alpha=0.16, select_alpha=0.16, good_text="#0ca30c",
)

LEVEL_COLOR = {"low": "good", "moderate": "warning", "high": "serious", "very_high": "critical"}
LEVEL_ICON = {"low": "✔", "moderate": "▲", "high": "⚠", "very_high": "✖"}


def status_color(p: Palette, level: str) -> str:
    return getattr(p, LEVEL_COLOR[level])


def apply_ttk_theme(root: tk.Tk, dark: bool) -> Palette:
    try:
        import sv_ttk

        sv_ttk.set_theme("dark" if dark else "light", root)
    except Exception:  # theme package missing or Tk too old: fall back to a stock theme
        style = ttk.Style(root)
        for candidate in ("vista", "clam"):
            if candidate in style.theme_names():
                style.theme_use(candidate)
                break
    palette = DARK if dark else LIGHT
    style = ttk.Style(root)
    bg = style.lookup("TFrame", "background") or palette.window
    palette = Palette(**{**palette.__dict__, "window": bg})
    root.configure(background=bg)
    style.configure("Muted.TLabel", foreground=palette.ink2)
    style.configure("Section.TLabel", font=fonts(root)["strong"])
    style.configure("Header.TLabel", font=fonts(root)["subtitle"])
    return palette


_FONT_CACHE: dict[str, tkfont.Font] = {}


def fonts(root: tk.Misc) -> dict[str, tkfont.Font]:
    """Named fonts, preferring the Sun Valley theme's Segoe UI Variable faces."""
    if _FONT_CACHE:
        return _FONT_CACHE
    available = set(tkfont.families(root))

    def pick(*families: str) -> str:
        for fam in families:
            if fam in available:
                return fam
        return "TkDefaultFont"

    text = pick("Segoe UI Variable Text", "Segoe UI", "Helvetica", "Arial")
    display = pick("Segoe UI Variable Display Semibold", "Segoe UI Semibold", text)
    strong = pick("Segoe UI Variable Text Semibold", "Segoe UI Semibold", text)
    small = pick("Segoe UI Variable Small", text)
    _FONT_CACHE.update(
        caption=tkfont.Font(root, family=small, size=9),
        body=tkfont.Font(root, family=text, size=10),
        strong=tkfont.Font(root, family=strong, size=10),
        subtitle=tkfont.Font(root, family=display, size=14),
        value=tkfont.Font(root, family=display, size=22),
        icon=tkfont.Font(root, family=pick("Segoe UI Symbol", text), size=11),
    )
    return _FONT_CACHE


def mpl_font_family() -> list[str]:
    """Preferred sans faces that are actually installed (avoids matplotlib 'findfont' warnings)."""
    from matplotlib import font_manager

    installed = {f.name for f in font_manager.fontManager.ttflist}
    families = [f for f in ("Segoe UI", "Helvetica", "Arial") if f in installed]
    return families + ["DejaVu Sans"]
