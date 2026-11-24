from __future__ import annotations

import tkinter as tk
from tkinter import ttk

import pytest
from PIL import Image, ImageDraw

from uv_dose.gui import theme


def test_grow_keeps_corners_and_stretches_the_middle():
    """Enlarging a drawn shape must match drawing it at the large size, for uniform middles."""
    small = Image.new("RGBA", (24, 24), (255, 255, 255, 255))
    ImageDraw.Draw(small).rounded_rectangle((0, 0, 23, 23), radius=6, fill=(200, 0, 0, 255))
    big = theme._grow(small, (8, 8, 8, 8), (100, 40))
    assert big.size == (100, 40)
    assert big.crop((0, 0, 8, 8)).tobytes() == small.crop((0, 0, 8, 8)).tobytes()  # corner kept
    assert big.crop((92, 32, 100, 40)).tobytes() == small.crop((16, 16, 24, 24)).tobytes()
    assert big.getpixel((50, 20)) == (200, 0, 0, 255)  # middle filled
    assert big.getpixel((50, 0)) == small.getpixel((12, 0))  # top edge stretched


@pytest.fixture
def root():
    # On Windows, starting a second Tk in one process now and then fails to find tk.tcl;
    # a retry works. No display at all is a skip.
    for attempt in range(3):
        try:
            r = tk.Tk()
            break
        except tk.TclError as exc:
            error = exc
    else:
        pytest.skip(f"Tk unavailable: {error}")
    r.withdraw()
    yield r
    r.destroy()


def test_themes_switch_and_cards_exist_in_both(root):
    light = theme.apply_ttk_theme(root, dark=False)
    assert light.name == "light" and ttk.Style(root).theme_use() == "uvdose-light"
    usual = theme.card_style(light.good)
    unusual = theme.card_style("#123456")  # made on demand
    frame = ttk.Frame(root, style=unusual)

    dark = theme.apply_ttk_theme(root, dark=True)
    assert dark.name == "dark" and ttk.Style(root).theme_use() == "uvdose-dark"
    style = ttk.Style(root)
    for name in (usual, unusual, "Surface.TButton", "Surface.Treeview", "Surface.Vertical.TScrollbar"):
        assert style.layout(name), name  # the switch didn't leave a widget without its style
    frame.update_idletasks()
    assert theme.apply_ttk_theme(root, dark=False) == light


def test_theme_images_are_opaque(root, monkeypatch):
    """See-through pixels make Tk repaint slowly (see the theme module docstring)."""
    drawn: list[Image.Image] = []
    real = theme.ImageTk.PhotoImage

    def capture(im, **kw):
        drawn.append(im)
        return real(im, **kw)

    monkeypatch.setattr(theme.ImageTk, "PhotoImage", capture)
    for dark in (False, True):
        theme.apply_ttk_theme(root, dark)
    assert len(drawn) > 50
    for im in drawn:
        assert im.mode == "RGBA"
        assert im.getextrema()[3] == (255, 255), im.size
