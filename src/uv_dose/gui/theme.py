"""Colours, fonts and the app's own ttk theme (light and dark).

The theme is built on ttk's plain "clam" theme. The rounded shapes (buttons, fields, tabs,
cards, scrollbar thumbs, radio buttons) are images drawn with Pillow the first time a theme
is used, at the display's scale. ttk stretches them as nine-slice images (fixed corners,
repeated edges and middle).

Two things keep that fast:

- The images are opaque, drawn on the colour they will sit on. Tk paints an image with
  semi-transparent pixels by reading back what's behind it and blending in software, for
  every repeat of every piece; with see-through corners, switching themes took 0.9 s.
  So widgets on the white chart panel use "Surface." styles, with images drawn on white.
- The images are larger than their corners need, so a big panel is a few repeats of the
  middle piece rather than thousands.
"""

from __future__ import annotations

import ctypes
import math
import sys
import tkinter as tk
from dataclasses import dataclass
from tkinter import font as tkfont
from tkinter import ttk
from typing import Callable

from PIL import Image, ImageDraw, ImageFilter, ImageTk

from uv_dose.dose import UV_CATEGORIES


@dataclass(frozen=True)
class Palette:
    name: str
    dark: bool
    window: str  # window background
    surface: str  # cards, panels, fields and the chart
    raised: str  # hovered buttons and tabs
    pressed: str
    border: str  # card and panel outlines
    control: str  # button and field outlines (a little stronger)
    ink: str
    ink2: str
    muted: str
    grid: str
    axis: str
    series: str  # primary data series (measured UV, dose on skin)
    forecast: str
    accent: str  # focus rings, selected radio buttons, the main button in a dialog
    accent_hover: str
    accent_pressed: str
    on_accent: str  # text on the accent colour
    accent_tint: str  # selected rows and text
    link: str
    shadow: float  # opacity of the cards' drop shadow
    band_alpha: float
    select_alpha: float
    # status colours (fixed): good / warning / serious / critical
    good: str = "#0ca30c"
    warning: str = "#fab219"
    serious: str = "#ec835a"
    critical: str = "#d03b3b"
    good_text: str = "#006300"


# Light: warm sand with a sunset-orange accent. Dark: deep dusk blue with an amber accent.
LIGHT = Palette(
    name="light", dark=False,
    window="#f7f4ef", surface="#ffffff", raised="#f2ede5", pressed="#e9e2d7",
    border="#e8e1d6", control="#d5ccbe",
    ink="#1f1b16", ink2="#5f5850", muted="#938a7e", grid="#efe9df", axis="#d5ccbe",
    series="#2a78d6", forecast="#938a7e",
    accent="#d9480f", accent_hover="#e8590c", accent_pressed="#bf3f0c", on_accent="#ffffff",
    accent_tint="#fdebe0", link="#c2410c",
    shadow=0.10, band_alpha=0.10, select_alpha=0.10,
)
DARK = Palette(
    name="dark", dark=True,
    window="#15171c", surface="#1d2027", raised="#272b34", pressed="#30353f",
    border="#2c3039", control="#3c414c",
    ink="#eef0f3", ink2="#b3b8c2", muted="#7f8592", grid="#272a32", axis="#3c414c",
    series="#4d9bf0", forecast="#a3a8b3",
    accent="#ff922b", accent_hover="#ffa94d", accent_pressed="#f76707", on_accent="#1a1206",
    accent_tint="#3d2a17", link="#ffa94d",
    shadow=0.40, band_alpha=0.16, select_alpha=0.16, good_text="#3cc63c",
)

LEVEL_COLOR = {"low": "good", "moderate": "warning", "high": "serious", "very_high": "critical"}
LEVEL_ICON = {"low": "✔", "moderate": "▲", "high": "⚠", "very_high": "✖"}


def status_color(p: Palette, level: str) -> str:
    return getattr(p, LEVEL_COLOR[level])


# ------------------------------------------------------------------ fonts

_FONT_CACHE: dict[str, tkfont.Font] = {}


def fonts(root: tk.Misc) -> dict[str, tkfont.Font]:
    """Named fonts, preferring Windows 11's Segoe UI Variable faces."""
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
        subtitle=tkfont.Font(root, family=display, size=15),
        value=tkfont.Font(root, family=display, size=22),
        icon=tkfont.Font(root, family=pick("Segoe UI Symbol", text), size=11),
    )
    # Entries, spinboxes and comboboxes take their font from the widget, not the theme.
    for name in ("TkDefaultFont", "TkTextFont"):
        tkfont.nametofont(name, root).configure(family=text, size=10)
    return _FONT_CACHE


def mpl_font_family() -> list[str]:
    """Preferred sans faces that are actually installed (avoids matplotlib 'findfont' warnings)."""
    from matplotlib import font_manager

    installed = {f.name for f in font_manager.fontManager.ttflist}
    families = [f for f in ("Segoe UI", "Helvetica", "Arial") if f in installed]
    return families + ["DejaVu Sans"]


# ------------------------------------------------------------------ drawing

SS = 4  # shapes are drawn this many times larger, then scaled down, for smooth edges


def _rgba(color: str, alpha: float = 1.0) -> tuple[int, int, int, int]:
    c = color.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16), round(255 * alpha)


class _Canvas:
    """A Pillow image drawn at SS x size; coordinates are in final pixels."""

    def __init__(self, w: int, h: int, bg: str | None = None) -> None:
        self.w, self.h = w, h
        self.im = Image.new("RGBA", (w * SS, h * SS), _rgba(bg) if bg else (0, 0, 0, 0))
        self.draw = ImageDraw.Draw(self.im)

    @staticmethod
    def box(x0: float, y0: float, x1: float, y1: float) -> tuple[float, float, float, float]:
        return x0 * SS, y0 * SS, x1 * SS - 1, y1 * SS - 1

    def rounded(self, x0: float, y0: float, x1: float, y1: float, r: float, fill: str,
                line: str | None = None, width: float = 1.0) -> None:
        """A rounded rectangle covering pixels x0..x1, y0..y1 (end exclusive), optionally outlined."""
        if line:
            self.draw.rounded_rectangle(self.box(x0, y0, x1, y1), radius=r * SS, fill=line)
            x0, y0, x1, y1, r = x0 + width, y0 + width, x1 - width, y1 - width, max(r - width, 0)
        self.draw.rounded_rectangle(self.box(x0, y0, x1, y1), radius=r * SS, fill=fill)

    def chevron(self, cx: float, cy: float, half_w: float, half_h: float, up: bool, color: str,
                width: float) -> None:
        dy = -half_h if up else half_h
        pts = [((cx - half_w) * SS, (cy - dy) * SS), (cx * SS, (cy + dy) * SS), ((cx + half_w) * SS, (cy - dy) * SS)]
        w = width * SS
        self.draw.line(pts, fill=color, width=round(w), joint="curve")
        for x, y in (pts[0], pts[2]):  # round caps
            self.draw.ellipse((x - w / 2, y - w / 2, x + w / 2, y + w / 2), fill=color)

    def image(self) -> Image.Image:
        # Scale down with premultiplied alpha, so edges don't pick up a dark fringe.
        return self.im.convert("RGBa").resize((self.w, self.h), Image.BOX).convert("RGBA")


def _grow(im: Image.Image, border: tuple[int, int, int, int], size: tuple[int, int]) -> Image.Image:
    """Enlarge a nine-slice image: keep the corners, stretch its middle row and column.

    Exact for the shapes here, whose middles are uniform; much cheaper than drawing large.
    """
    (w, h), (big_w, big_h) = im.size, size
    left, top, right, bottom = border
    cols = ((0, left, 0, left), (w // 2, w // 2 + 1, left, big_w - right), (w - right, w, big_w - right, big_w))
    rows = ((0, top, 0, top), (h // 2, h // 2 + 1, top, big_h - bottom), (h - bottom, h, big_h - bottom, big_h))
    out = Image.new(im.mode, size)
    for sx0, sx1, dx0, dx1 in cols:
        for sy0, sy1, dy0, dy1 in rows:
            if dx1 > dx0 and dy1 > dy0:
                out.paste(im.crop((sx0, sy0, sx1, sy1)).resize((dx1 - dx0, dy1 - dy0), Image.NEAREST), (dx0, dy0))
    return out


# ------------------------------------------------------------------ the theme


class _Theme:
    """One ttk theme ("uvdose-light" or "uvdose-dark"): its images, styles and card styles."""

    def __init__(self, root: tk.Tk, palette: Palette) -> None:
        self.root = root
        self.p = palette
        self.name = f"uvdose-{palette.name}"
        self.scale = max(root.winfo_fpixels("1i") / 96, 1.0)
        self.style = ttk.Style(root)
        self._images: list[ImageTk.PhotoImage] = []  # Tk drops images Python no longer references
        self._cards: set[str] = set()
        self.style.theme_create(self.name, parent="clam")
        self._in_theme(self._build)
        # Defining a style later makes Tk lay out and repaint every widget again, so the cards'
        # usual colours (status levels and UV categories) are made now.
        for stripe in (None, palette.good, palette.warning, palette.serious, palette.critical,
                       *(c.color for c in UV_CATEGORIES)):
            self.card_style(stripe)

    # ---------------------------------------------------------- helpers

    def px(self, n: float) -> int:
        return max(1, round(n * self.scale))

    def photo(self, canvas: _Canvas | Image.Image, grow: tuple[int, int] | None = None,
              border: int | tuple[int, ...] = 0) -> ImageTk.PhotoImage:
        """A Tk image of the drawing, optionally enlarged to `grow` (see _grow) around `border`."""
        im = canvas.image() if isinstance(canvas, _Canvas) else canvas
        if grow:
            b = (border,) * 4 if isinstance(border, int) else tuple(border)
            im = _grow(im, b * 2 if len(b) == 2 else b, grow)
        ph = ImageTk.PhotoImage(im, master=self.root)
        self._images.append(ph)
        return ph

    def _in_theme(self, fn: Callable[[], None]) -> None:
        """Run fn with this theme current for definitions, without switching the window to it.

        Creating styles in a theme before it is used means no widget ever looks for a style
        the theme doesn't have yet (which Tk reports as an error).
        """
        failure: list[BaseException] = []

        def run() -> None:
            try:
                fn()
            except BaseException as exc:  # noqa: BLE001 - re-raised below, outside Tcl
                failure.append(exc)

        cmd = self.root.register(run)
        try:
            self.root.tk.call("ttk::style", "theme", "settings", self.name, cmd)
        finally:
            self.root.deletecommand(cmd)
        if failure:
            raise failure[0]

    def use(self) -> None:
        self.style.theme_use(self.name)

    # ---------------------------------------------------------- shapes
    # Every image is drawn opaque, on the colour it will sit on, and fairly large: see the
    # module docstring for why.

    def _control(self, fill: str, line: str, bg: str) -> ImageTk.PhotoImage:
        """Button and field face: a rounded box with a 1 px outline."""
        n, r = self.px(24), 6 * self.scale
        c = _Canvas(n, n, bg)
        c.rounded(0, 0, n, n, r, fill, line, self.scale)
        return self.photo(c, (self.px(96), self.px(40)), self.px(8))

    def _card(self, stripe: str | None, size: int = 160) -> ImageTk.PhotoImage:
        """A card: rounded, outlined, a soft shadow below, and an optional status stripe on the left."""
        p, m = self.p, self.card_margins()
        big, size, r = self.px(size), self.px(64), 10 * self.scale
        x0, y0, x1, y1 = m[0], m[1], size - m[2], size - m[3]
        out = Image.new("RGBA", (size, size), _rgba(p.window))
        if p.shadow:  # soft, so it's drawn at final size (blurring the large drawing is slow)
            mask = Image.new("L", out.size, 0)
            drop = round(self.scale * 1.5)
            ImageDraw.Draw(mask).rounded_rectangle((x0 + 1, y0 + drop, x1 - 2, y1 - 1 + drop), radius=r,
                                                   fill=round(255 * p.shadow))
            mask = mask.filter(ImageFilter.GaussianBlur(2.2 * self.scale))
            out.paste(Image.new("RGBA", out.size, _rgba("#000000" if p.dark else "#3a2a1a")), (0, 0), mask)
        card = _Canvas(size, size)
        card.rounded(x0, y0, x1, y1, r, p.surface, p.border, self.scale)
        if stripe:
            shape = Image.new("L", card.im.size, 0)
            ImageDraw.Draw(shape).rounded_rectangle(_Canvas.box(x0, y0, x1, y1), radius=r * SS, fill=255)
            shape.paste(0, (round((x0 + self.stripe_width()) * SS), 0, shape.width, shape.height))
            card.im.paste(Image.new("RGBA", card.im.size, _rgba(stripe)), (0, 0), shape)
        out.alpha_composite(card.image())
        return self.photo(out, (big, big), self._slices())

    def card_margins(self) -> tuple[int, int, int, int]:
        """Room around a card's box for its shadow: left, top, right, bottom."""
        return self.px(3), self.px(1), self.px(3), self.px(5)

    def stripe_width(self) -> int:
        return self.px(4)

    def _slices(self) -> tuple[int, int, int, int]:
        """The nine-slice borders of a card image: the corners plus the shadow margins."""
        m, r = self.card_margins(), math.ceil(10 * self.scale) + 1
        return m[0] + r, m[1] + r, m[2] + r, m[3] + r

    def card_style(self, stripe: str | None) -> str:
        """The ttk style for a card with this stripe colour (None for no stripe), made on first use.

        Any colour works; the ones made when the theme is built cost nothing later.
        """
        name = f"Card{(stripe or 'none').lstrip('#')}.TFrame"
        if name not in self._cards:
            self._cards.add(name)
            self._in_theme(lambda: self._make_card_style(name, stripe))
        return name

    def _make_card_style(self, name: str, stripe: str | None) -> None:
        element = name.replace(".TFrame", ".face")
        b = self._slices()
        self.style.element_create(element, "image", self._card(stripe), border=b, sticky="nsew",
                                  width=b[0] + b[2], height=b[1] + b[3])
        self.style.layout(name, [(element, {"sticky": "nsew"})])
        self.style.configure(name, background=self.p.window)

    # ---------------------------------------------------------- styles

    def _build(self) -> None:
        p, s, px = self.p, self.style, self.px
        f = fonts(self.root)
        cut, edge = px(8), px(1)
        W, S = p.window, p.surface

        s.configure(".", background=W, foreground=p.ink, font=f["body"], bordercolor=p.control,
                    lightcolor=W, darkcolor=W, troughcolor=W, focuscolor=p.accent,
                    selectbackground=p.accent_tint, selectforeground=p.ink, insertcolor=p.ink,
                    fieldbackground=S, insertwidth=1)
        s.map(".", foreground=[("disabled", p.muted)])
        for name in ("TButton", "TMenubutton", "TRadiobutton", "TCheckbutton", "TCombobox", "TEntry", "TSpinbox",
                     "TNotebook.Tab", "Treeview.Heading", "TScrollbar", "Vertical.TScrollbar"):
            s.map(name, background=[])  # clam's hover colours

        # frames and labels
        s.configure("Muted.TLabel", foreground=p.ink2)
        s.configure("Section.TLabel", font=f["strong"])
        s.configure("Header.TLabel", font=f["subtitle"])
        for name in ("Surface.TFrame", "Surface.TLabel", "Surface.TButton", "Surface.Vertical.TScrollbar"):
            s.configure(name, background=S)
        s.configure("Muted.Surface.TLabel", foreground=p.ink2)
        s.configure("TSeparator", background=p.border)

        def nine(element: str, normal, *states, border, padding=edge, **kw) -> None:
            """A stretchable image element that can shrink to its corners."""
            b = (border,) * 4 if isinstance(border, int) else border
            b = b * 2 if len(b) == 2 else b
            kw.setdefault("width", b[0] + b[2])
            kw.setdefault("height", b[1] + b[3])
            s.element_create(element, "image", normal, *states, border=border, padding=padding, sticky="nsew",
                             **kw)

        # buttons
        def button_face(element: str, bg: str, fills: tuple[str, str, str, str], lines: tuple[str, str, str, str],
                        disabled: tuple[str, str]) -> None:
            normal, hover, pressed, focus = (self._control(fl, ln, bg) for fl, ln in zip(fills, lines))
            nine(element, normal, ("disabled", self._control(*disabled, bg)), ("pressed", pressed),
                 ("active", hover), ("focus", focus), border=cut)

        for prefix, bg in (("", W), ("Surface.", S)):
            button_face(f"{prefix}Button.face", bg, (S, p.raised, p.pressed, S),
                        (p.control, p.control, p.control, p.accent), (W, p.border))
        button_face("Accent.Button.face", W, (p.accent, p.accent_hover, p.accent_pressed, p.accent),
                    (p.accent, p.accent_hover, p.accent_pressed, p.accent_pressed), (p.pressed, p.pressed))
        for style_name, element in (("TButton", "Button.face"), ("Surface.TButton", "Surface.Button.face"),
                                    ("Accent.TButton", "Accent.Button.face")):
            s.layout(style_name, [(element, {"sticky": "nsew", "children": [
                ("Button.padding", {"sticky": "nsew", "children": [("Button.label", {"sticky": "nsew"})]})]})])
        s.configure("TButton", padding=(px(12), px(4)), anchor="center")
        s.configure("Accent.TButton", foreground=p.on_accent)
        s.map("Accent.TButton", foreground=[("disabled", p.muted)])
        # a menu button that looks like the buttons, without the drop-down indicator
        s.layout("TMenubutton", [("Button.face", {"sticky": "nsew", "children": [
            ("Menubutton.padding", {"sticky": "nsew", "children": [("Menubutton.label", {"sticky": ""})]})]})])
        s.configure("TMenubutton", padding=(px(10), px(4)))

        # text fields: entry, spinbox and (read-only) combobox share one face
        nine("Field.face", self._control(S, p.control, W), ("disabled", self._control(W, p.border, W)),
             ("focus", self._control(S, p.accent, W)), ("hover", self._control(S, p.muted, W)), border=cut)
        chevron_w, chevron_h = 3.6 * self.scale, 1.9 * self.scale

        def arrow(w: int, h: int, up: bool, color: str, pill: str | None = None, bg: str = S) -> ImageTk.PhotoImage:
            c = _Canvas(w, h, bg)
            if pill:
                c.rounded(px(1), px(3), w - px(1), h - px(3), 4 * self.scale, pill)
            c.chevron(w / 2, h / 2, chevron_w, chevron_h, up, color, 1.35 * self.scale)
            return self.photo(c)

        aw, ah = px(26), px(22)
        s.element_create("Combobox.downarrow", "image", arrow(aw, ah, False, p.ink2),
                         ("disabled", arrow(aw, ah, False, p.muted, bg=W)), ("pressed", arrow(aw, ah, False, p.accent)),
                         ("hover", arrow(aw, ah, False, p.ink)), sticky="")
        sw, sh = px(20), px(24)
        for direction, up in (("uparrow", True), ("downarrow", False)):
            s.element_create(f"Spinbox.{direction}", "image", arrow(sw, sh, up, p.ink2),
                             ("disabled", arrow(sw, sh, up, p.muted, bg=W)),
                             ("pressed", arrow(sw, sh, up, p.accent, p.pressed)),
                             ("active", arrow(sw, sh, up, p.ink, p.raised)), sticky="")
        text_pad = (px(8), px(4), px(4), px(4))
        s.layout("TEntry", [("Field.face", {"sticky": "nsew", "children": [
            ("Entry.padding", {"sticky": "nsew", "children": [("Entry.textarea", {"sticky": "nsew"})]})]})])
        s.layout("TCombobox", [("Field.face", {"sticky": "nsew", "children": [
            ("Combobox.downarrow", {"side": "right", "sticky": "ns"}),
            ("Combobox.padding", {"sticky": "nsew", "children": [("Combobox.textarea", {"sticky": "nsew"})]})]})])
        s.layout("TSpinbox", [("Field.face", {"sticky": "nsew", "children": [
            ("Spinbox.downarrow", {"side": "right", "sticky": "ns"}),
            ("Spinbox.uparrow", {"side": "right", "sticky": "ns"}),
            ("Spinbox.padding", {"sticky": "nsew", "children": [("Spinbox.textarea", {"sticky": "nsew"})]})]})])
        for name in ("TEntry", "TCombobox", "TSpinbox"):
            s.configure(name, padding=text_pad, foreground=p.ink)
        # A read-only combobox shows its text selected while focused; keep it plain.
        s.configure("TCombobox", selectbackground=S, selectforeground=p.ink)
        s.map("TCombobox", selectbackground=[], selectforeground=[], fieldbackground=[], foreground=[
            ("disabled", p.muted)])
        s.configure("ComboboxPopdownFrame", background=S, bordercolor=p.control, lightcolor=S, darkcolor=S,
                    relief="solid", borderwidth=1)

        # radio buttons
        def radio(ring: str, fill: str, dot: str | None) -> ImageTk.PhotoImage:
            d, gap = px(18), px(8)
            c = _Canvas(d + gap, d, W)
            c.draw.ellipse(c.box(0.5, 0.5, d - 0.5, d - 0.5), fill=ring)
            w = self.scale
            c.draw.ellipse(c.box(0.5 + w, 0.5 + w, d - 0.5 - w, d - 0.5 - w), fill=fill)
            if dot:
                q = d * 0.32
                c.draw.ellipse(c.box(q, q, d - q, d - q), fill=dot)
            return self.photo(c)

        s.element_create("Radiobutton.indicator", "image", radio(p.control, S, None),
                         ("disabled", "selected", radio(p.border, W, p.muted)),
                         ("disabled", radio(p.border, W, None)),
                         ("pressed", "selected", radio(p.accent_pressed, p.accent_pressed, p.on_accent)),
                         ("active", "selected", radio(p.accent_hover, p.accent_hover, p.on_accent)),
                         ("selected", radio(p.accent, p.accent, p.on_accent)),
                         ("pressed", radio(p.muted, p.pressed, None)),
                         ("active", radio(p.ink2, p.raised, None)), sticky="")
        s.layout("TRadiobutton", [("Radiobutton.padding", {"sticky": "nsew", "children": [
            ("Radiobutton.indicator", {"side": "left", "sticky": ""}),
            ("Radiobutton.label", {"side": "left", "sticky": "nsew"})]})])
        s.configure("TRadiobutton", padding=(0, px(2)))

        # scrollbars: a slim rounded thumb, no arrows
        tw, th = px(12), px(80)
        for prefix, bg in (("", W), ("Surface.", S)):
            def thumb(color: str, bg: str = bg) -> ImageTk.PhotoImage:
                c = _Canvas(tw, px(16), bg)
                c.rounded(px(3), 0, tw - px(3), px(16), 3 * self.scale, color)
                return self.photo(c, (tw, th), (0, px(5)))

            trough, bar = f"{prefix}Vertical.Scrollbar.trough", f"{prefix}Vertical.Scrollbar.thumb"
            nine(trough, self.photo(Image.new("RGBA", (tw, th), _rgba(bg))), border=0, padding=0, width=tw,
                 height=0)
            nine(bar, thumb(p.control), ("pressed", thumb(p.ink2)), ("active", thumb(p.muted)),
                 border=(0, px(5)), padding=0, width=tw)
            s.layout(f"{prefix}Vertical.TScrollbar", [(trough, {"sticky": "ns", "children": [
                (bar, {"expand": True, "sticky": "nsew"})]})])

        # notebook: tabs are chips above a card
        m = self.card_margins()
        inset = px(4)
        nine("Notebook.client", self._card(None, size=240), border=self._slices(),
             padding=(m[0] + inset, m[1] + inset, m[2] + inset, m[3] + inset))
        s.layout("TNotebook", [("Notebook.client", {"sticky": "nsew"})])
        s.configure("TNotebook", tabmargins=(m[0], 0, 0, px(6)), padding=0)

        gap = px(4)

        tab_border = (px(9), px(9), px(9) + gap, px(9))

        def tab(fill: str | None, line: str | None) -> ImageTk.PhotoImage:
            w, h = px(28) + gap, px(28)
            c = _Canvas(w, h, W)
            if fill:
                c.rounded(0, 0, w - gap, h, 7 * self.scale, fill, line, self.scale)
            return self.photo(c, (px(120) + gap, px(40)), tab_border)

        nine("Notebook.tab", tab(None, None), ("selected", tab(S, p.border)), ("hover", tab(p.raised, None)),
             border=tab_border, padding=0)
        s.layout("TNotebook.Tab", [("Notebook.tab", {"sticky": "nsew", "children": [
            ("Notebook.padding", {"side": "top", "sticky": "nsew", "children": [
                ("Notebook.label", {"side": "top", "sticky": ""})]})]})])
        s.configure("TNotebook.Tab", padding=(px(14), px(5), px(14) + gap, px(5)), foreground=p.ink2)
        s.map("TNotebook.Tab", foreground=[("selected", p.ink)], font=[("selected", f["strong"])], expand=[],
              padding=[])

        # tree views: a rounded box
        for prefix, bg in (("", W), ("Surface.", S)):
            field, n = f"{prefix}Treeview.field", px(24)
            c = _Canvas(n, n, bg)
            c.rounded(0, 0, n, n, 6 * self.scale, S, p.border, self.scale)
            nine(field, self.photo(c, (px(200), px(200)), cut), border=cut, padding=px(3))
            s.layout(f"{prefix}Treeview", [(field, {"sticky": "nsew", "children": [
                ("Treeview.padding", {"sticky": "nsew", "children": [("Treeview.treearea", {"sticky": "nsew"})]})]})])
        s.configure("Treeview", background=S, fieldbackground=S, foreground=p.ink, rowheight=px(26), borderwidth=0)
        s.map("Treeview", background=[("selected", p.accent_tint)], foreground=[("selected", p.ink)])
        s.configure("Treeview.Heading", background=S, foreground=p.ink2, font=f["strong"], relief="flat",
                    borderwidth=0, lightcolor=S, darkcolor=S, bordercolor=S, padding=(px(6), px(3)))
        s.map("Treeview.Heading", background=[], foreground=[("active", p.ink)])


_THEMES: dict[str, _Theme] = {}
_current: _Theme | None = None


def apply_ttk_theme(root: tk.Tk, dark: bool) -> Palette:
    """Switch the window to the light or dark theme (building it the first time) and return its palette."""
    global _current
    palette = DARK if dark else LIGHT
    theme = _THEMES.get(palette.name)
    if theme is None or theme.root is not root:
        theme = _THEMES[palette.name] = _Theme(root, palette)
    if _current is not None and _current.root is root:
        for name in _current._cards:  # the cards already shown need their styles before the switch
            theme.card_style(None if name == "Cardnone.TFrame" else "#" + name[4:-7])
    theme.use()
    _current = theme
    root.configure(background=palette.window)
    _style_popdowns(root, palette)
    style_title_bar(root, palette)
    return palette


def card_style(stripe: str | None) -> str:
    """The style for a ResultCard with this stripe colour, in the current theme."""
    assert _current is not None, "apply_ttk_theme() first"
    return _current.card_style(stripe)


def card_insets() -> tuple[int, int, int, int]:
    """Where a card's contents start, inside its shadow, outline and stripe: left, top, right, bottom."""
    assert _current is not None, "apply_ttk_theme() first"
    t = _current
    m, pad = t.card_margins(), t.px(12)
    return m[0] + t.stripe_width() + pad, m[1] + t.px(9), m[2] + pad, m[3] + t.px(9)


def style_menu(menu: tk.Menu, p: Palette) -> None:
    f = fonts(menu)
    menu.configure(background=p.surface, foreground=p.ink, activebackground=p.accent_tint,
                   activeforeground=p.ink, selectcolor=p.accent, disabledforeground=p.muted,
                   font=f["body"], relief="flat", borderwidth=0, activeborderwidth=0)


def _style_popdowns(root: tk.Tk, p: Palette) -> None:
    """Colour the combobox drop-down lists: new ones via the option database, open ones directly."""
    opts = {"background": p.surface, "foreground": p.ink, "selectBackground": p.accent_tint,
            "selectForeground": p.ink, "font": str(fonts(root)["body"]), "relief": "flat", "borderWidth": 0,
            "highlightThickness": 0, "activeStyle": "none"}
    for key, value in opts.items():
        root.option_add(f"*TCombobox*Listbox.{key}", value)
    stack: list[tk.Misc] = [root]
    while stack:
        w = stack.pop()
        stack.extend(w.winfo_children())
        if isinstance(w, ttk.Combobox) and root.tk.call("winfo", "exists", f"{w}.popdown"):
            listbox = root.tk.call("ttk::combobox::PopdownWindow", w) + ".f.l"
            root.tk.call(listbox, "configure", *[a for k, v in opts.items() for a in (f"-{k.lower()}", v)])


# ------------------------------------------------------------------ window dressing


def style_title_bar(window: tk.Wm, p: Palette) -> None:
    """Colour the window's title bar to match the theme (Windows 11; ignored elsewhere)."""
    if sys.platform != "win32":
        return

    def apply() -> None:
        try:
            hwnd = int(window.wm_frame(), 16)
            dwm = ctypes.windll.dwmapi

            def put(attribute: int, value: int) -> None:
                v = ctypes.c_int(value)
                dwm.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(v), ctypes.sizeof(v))

            def colorref(color: str) -> int:
                r, g, b, _ = _rgba(color)
                return r | g << 8 | b << 16

            put(20, int(p.dark))  # DWMWA_USE_IMMERSIVE_DARK_MODE: light or dark caption buttons
            put(35, colorref(p.window))  # DWMWA_CAPTION_COLOR
            put(36, colorref(p.ink))  # DWMWA_TEXT_COLOR
            put(34, colorref(p.border))  # DWMWA_BORDER_COLOR
        except (OSError, AttributeError, ValueError, tk.TclError):
            pass

    if window.winfo_ismapped():
        apply()
    else:  # the title bar only exists once the window is shown
        done = []

        def on_map(event: tk.Event) -> None:
            if event.widget is window and not done:
                done.append(True)
                apply()

        window.bind("<Map>", on_map, add="+")


_LOGOS: dict[tuple[str, int], ImageTk.PhotoImage] = {}


def sun_logo(root: tk.Misc, size: int = 24) -> ImageTk.PhotoImage:
    """A small sun: an amber disc with eight orange rays."""
    size = max(8, round(size * max(root.winfo_fpixels("1i") / 96, 1.0)))
    key = (str(root), size)
    if key not in _LOGOS:
        c = _Canvas(size, size)
        mid = size / 2
        for i in range(8):
            a = math.pi / 4 * i
            x0, y0 = mid + math.cos(a) * size * 0.34, mid + math.sin(a) * size * 0.34
            x1, y1 = mid + math.cos(a) * size * 0.45, mid + math.sin(a) * size * 0.45
            c.draw.line([(x0 * SS, y0 * SS), (x1 * SS, y1 * SS)], fill="#f76707", width=round(size * 0.09 * SS))
            for x, y in ((x0, y0), (x1, y1)):
                r = size * 0.045
                c.draw.ellipse(c.box(x - r, y - r, x + r, y + r), fill="#f76707")
        r = size * 0.25
        c.draw.ellipse(c.box(mid - r, mid - r, mid + r, mid + r), fill="#ffb000")
        _LOGOS[key] = ImageTk.PhotoImage(c.image(), master=root)
    return _LOGOS[key]


def uv_gradient(width: int, height: int, colors: list[str]) -> Image.Image:
    """A left-to-right blend through the given colours."""
    strip = Image.new("RGB", (len(colors), 1))
    strip.putdata([_rgba(c)[:3] for c in colors])
    # Resizing a strip with one pixel per colour blends evenly between their centres.
    return strip.resize((width, height), Image.BILINEAR)
