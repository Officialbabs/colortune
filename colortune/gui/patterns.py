"""Test patterns (drawn with cairo) and the full-screen guided calibration window."""
import colorsys
import math

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gdk, Gtk  # noqa: E402

from ..backends import screenfilters  # noqa: E402
from .widgets import SliderRow, label  # noqa: E402

# step id, title, short description, instructions, pattern
GUIDE = [
    ("black", "Black level", "Deep blacks without losing shadow detail",
     "The background is pure black. The squares are the first 20 steps above it.\n"
     "Aim for the darkest background where squares 3–4 are still just visible.\n"
     "1–2 clearly visible → blacks are greyish: lower Backlight/Brightness or move Black point below 0.\n"
     "5–6 invisible → shadows are crushed: raise Black point or Brightness.", "black"),
    ("white", "White level", "Bright detail that doesn't clip",
     "The background is pure white. The squares are steps 235–254.\n"
     "Every square should stay distinguishable. If the top ones vanish, lower Contrast or White level.", "white"),
    ("gamma", "Gamma", "Mid-tones at the right brightness",
     "Step back or squint until the fine stripes blur into grey.\n"
     "Adjust Gamma until the 2.2 patch blends into its stripes (2.4 for a dark room).\n"
     "Exact only at 100% display scaling; with fractional scaling, judge the smooth ramp.", "gamma"),
    ("white_balance", "White balance", "Neutral greys with no colour cast",
     "Every grey step should look neutral: no pink, green or blue cast.\n"
     "Lower the gain of whichever colour you see too much of. Glance at white paper in daylight "
     "between checks, because your eyes adapt quickly.", "grey"),
    ("saturation", "Saturation", "Rich colour that keeps its gradations",
     "Colours should look rich, but the bright end of each gradient must not flatten into one band, "
     "and skin tones shouldn't look orange.\n"
     "The colour bars show 100% and 75% intensity. On a limited-gamut panel even 100% looks muted, "
     "and that's the panel, not the setting.", "color"),
    ("uniformity", "Uniformity", "Backlight bleed, glow and tint",
     "Full-screen fields for spotting backlight bleed, IPS glow, tint shifts and dead pixels.\n"
     "Hide this panel (H) and use ←/→ to cycle colours.", "solid"),
]

SOLIDS = [(0, 0, 0), (1, 1, 1), (0.5, 0.5, 0.5), (0.2, 0.2, 0.2), (1, 0, 0), (0, 1, 0), (0, 0, 1)]


# ---------------------------------------------------------------- drawing

def _text(cr, text, x, y, size, rgb):
    cr.select_font_face("Sans")
    cr.set_font_size(size)
    ext = cr.text_extents(text)
    cr.set_source_rgb(*rgb)
    cr.move_to(x - ext.width / 2 - ext.x_bearing, y)
    cr.show_text(text)


def _squares(cr, w, h, bg, levels, label_rgb, labels=True):
    cr.set_source_rgb(bg, bg, bg)
    cr.paint()
    cols = 10
    rows = math.ceil(len(levels) / cols)
    size = min(w / (cols + 2), h * 0.55 / (rows + 1))
    gap = size * 0.18
    x0 = (w - cols * size) / 2
    y0 = h * 0.12
    for i, lv in enumerate(levels):
        x = x0 + (i % cols) * size
        y = y0 + (i // cols) * (size + gap * 3)
        cr.set_source_rgb(lv / 255, lv / 255, lv / 255)
        cr.rectangle(x + gap / 2, y, size - gap, size - gap)
        cr.fill()
        if labels:
            _text(cr, str(lv), x + size / 2, y + size + gap, size * 0.22, label_rgb)


def _ramp(cr, x, y, rw, rh, rgb, steps=None):
    if steps:
        for i in range(steps):
            v = i / (steps - 1)
            cr.set_source_rgb(rgb[0] * v, rgb[1] * v, rgb[2] * v)
            cr.rectangle(x + i * rw / steps, y, rw / steps + 0.5, rh)
            cr.fill()
        return
    grad = cairo.LinearGradient(x, 0, x + rw, 0)
    grad.add_color_stop_rgb(0, 0, 0, 0)
    grad.add_color_stop_rgb(1, *rgb)
    cr.set_source(grad)
    cr.rectangle(x, y, rw, rh)
    cr.fill()


def p_black(cr, w, h, scale=1.0, labels=True):
    _squares(cr, w, h, 0.0, list(range(1, 21)), (0.25, 0.25, 0.25), labels)


def p_white(cr, w, h, scale=1.0, labels=True):
    _squares(cr, w, h, 1.0, list(range(235, 255)), (0.6, 0.6, 0.6), labels)


def p_gamma(cr, w, h, scale=1.0, labels=True):
    cr.set_source_rgb(0, 0, 0)
    cr.paint()
    gammas = [1.8, 2.0, 2.2, 2.4, 2.6]
    s = scale
    cr.save()
    cr.scale(1 / s, 1 / s)  # device pixels, so the stripes are exactly 1px
    W, H = w * s, h * s
    bw = W / (len(gammas) + 1)
    top, bh = int(H * 0.08), int(H * 0.5)
    for i, g in enumerate(gammas):
        x = int(bw / 2 + i * bw)
        cr.set_source_rgb(1, 1, 1)
        for yy in range(top, top + bh, 2):
            cr.rectangle(x, yy, int(bw * 0.9), 1)
        cr.fill()
        v = 0.5 ** (1 / g)
        cr.set_source_rgb(v, v, v)
        cr.rectangle(x + bw * 0.25, top + bh * 0.3, bw * 0.4, bh * 0.4)
        cr.fill()
    cr.restore()
    if labels:
        for i, g in enumerate(gammas):
            _text(cr, f"{g}", (bw / 2 + i * bw + bw * 0.45) / s, (top + bh) / s + 28, 22, (0.8, 0.8, 0.8))
    _ramp(cr, w * 0.06, h * 0.66, w * 0.88, h * 0.05, (1, 1, 1))


def p_grey(cr, w, h, scale=1.0, labels=True):
    cr.set_source_rgb(0.5, 0.5, 0.5)
    cr.paint()
    x, rw = w * 0.04, w * 0.92
    _ramp(cr, x, h * 0.05, rw, h * 0.14, (1, 1, 1))
    _ramp(cr, x, h * 0.21, rw, h * 0.14, (1, 1, 1), steps=17)
    for i, rgb in enumerate([(1, 0, 0), (0, 1, 0), (0, 0, 1)]):
        _ramp(cr, x, h * (0.37 + i * 0.07), rw, h * 0.06, rgb, steps=32)


def p_color(cr, w, h, scale=1.0, labels=True):
    cr.set_source_rgb(0.1, 0.1, 0.1)
    cr.paint()
    bars = [(1, 1, 1), (1, 1, 0), (0, 1, 1), (0, 1, 0), (1, 0, 1), (1, 0, 0), (0, 0, 1)]
    bw = w / len(bars)
    for row, level in enumerate((1.0, 0.75)):
        for i, c in enumerate(bars):
            cr.set_source_rgb(*(v * level for v in c))
            cr.rectangle(i * bw, h * (0.02 + row * 0.14), bw + 0.5, h * 0.13)
            cr.fill()
    top, rows, cols = h * 0.31, 12, 64
    rh = h * 0.27 / rows
    for r in range(rows):
        for c in range(cols):
            cr.set_source_rgb(*colorsys.hsv_to_rgb(r / rows, c / (cols - 1), 1.0))
            cr.rectangle(w * 0.04 + c * w * 0.92 / cols, top + r * rh, w * 0.92 / cols + 0.5, rh - (1 if rh > 4 else 0))
            cr.fill()
    skins = [(0.98, 0.87, 0.78), (0.94, 0.76, 0.62), (0.85, 0.64, 0.49), (0.71, 0.5, 0.36),
             (0.55, 0.37, 0.26), (0.38, 0.25, 0.17)]
    sw = w * 0.92 / len(skins)
    for i, c in enumerate(skins):
        cr.set_source_rgb(*c)
        cr.rectangle(w * 0.04 + i * sw, h * 0.59, sw - max(1, w / 400), h * 0.06)
        cr.fill()


def p_solid(cr, w, h, scale=1.0, labels=True, index=0):
    cr.set_source_rgb(*SOLIDS[index % len(SOLIDS)])
    cr.paint()


def p_solid_thumb(cr, w, h, scale=1.0, labels=True):
    """Thumbnail: the solid fields side by side."""
    n = len(SOLIDS)
    for i, c in enumerate(SOLIDS):
        cr.set_source_rgb(*c)
        cr.rectangle(i * w / n, 0, w / n + 0.5, h)
        cr.fill()


PATTERNS = {"black": p_black, "white": p_white, "gamma": p_gamma, "grey": p_grey, "color": p_color,
            "solid": p_solid}


def thumbnail(kind, width=232, height=130):
    area = Gtk.DrawingArea(content_width=width, content_height=height)
    area.add_css_class("ct-thumb")
    area.set_overflow(Gtk.Overflow.HIDDEN)
    fn = p_solid_thumb if kind == "solid" else PATTERNS[kind]

    def draw(_a, cr, w, h):
        # draw at a "screen" size, scaled down, so proportions match the real thing
        vw = 960
        cr.scale(w / vw, w / vw)
        fn(cr, vw, vw * h / w, 1.0, False)
    area.set_draw_func(draw)
    return area


# ---------------------------------------------------------------- window

class PatternWindow(Gtk.Window):
    def __init__(self, app, controls, on_change=None, start="black", solid=None, message=None,
                 single=False):
        super().__init__(application=app, title="ColorTune patterns")
        self.set_default_size(1280, 800)
        self.controls = controls
        self.on_change = on_change
        self.step = next((i for i, g in enumerate(GUIDE) if g[0] == start), 0)
        self.single = single
        self.solid_idx = 0
        self.solid = solid
        self.message = message

        overlay = Gtk.Overlay()
        self.area = Gtk.DrawingArea(hexpand=True, vexpand=True)
        self.area.set_draw_func(self._draw)
        overlay.set_child(self.area)
        self.panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                             halign=Gtk.Align.CENTER, valign=Gtk.Align.END, margin_bottom=28,
                             margin_start=16, margin_end=16)
        self.panel.add_css_class("ct-panel")
        self.panel.set_size_request(820, -1)
        overlay.add_overlay(self.panel)
        self.set_child(overlay)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._key)
        self.add_controller(keys)
        click = Gtk.GestureClick()
        click.connect("pressed", lambda *a: self.panel.set_visible(True))
        self.area.add_controller(click)
        self._build_panel()
        self.fullscreen()

    def _build_panel(self):
        while (c := self.panel.get_first_child()):
            self.panel.remove(c)
        if self.solid is not None:
            self.status = label(self.message or "", "ct-panel-title", wrap=True)
            self.panel.append(self.status)
            self.panel.append(label("Esc cancels", "ct-tip"))
            return
        sid, title, _short, text, _ = GUIDE[self.step]

        head = Gtk.Box(spacing=12)
        t = label(title, "ct-panel-title")
        t.set_hexpand(True)
        head.append(t)
        if not self.single:
            dots = Gtk.Box(spacing=5, valign=Gtk.Align.CENTER)
            for i in range(len(GUIDE)):
                d = Gtk.Box()
                d.add_css_class("ct-dot")
                if i == self.step:
                    d.add_css_class("active")
                dots.append(d)
            head.append(dots)
        self.panel.append(head)
        self.panel.append(label(text, wrap=True))

        nl = screenfilters.night_light()
        if nl:
            warn = Gtk.Box(spacing=10)
            warn.add_css_class("ct-warn")
            msg = label(f"{nl['detail']}, so colours here look warmer than they are.", wrap=True)
            msg.set_hexpand(True)
            warn.append(msg)
            if nl["can_pause"]:
                b = Gtk.Button(label="Pause it", valign=Gtk.Align.CENTER)
                b.connect("clicked", lambda *_: (screenfilters.pause_night_light(), self._build_panel()))
                warn.append(b)
            self.panel.append(warn)

        relevant = [c for c in self.controls if sid in c.steps]
        if relevant:
            cols = Gtk.Box(spacing=12, homogeneous=len(relevant) > 1)
            lists = [Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE) for _ in range(min(2, len(relevant)))]
            for lb in lists:
                lb.add_css_class("boxed-list")
                lb.set_valign(Gtk.Align.START)
                lb.set_hexpand(True)
                cols.append(lb)
            for i, c in enumerate(relevant):
                lists[i % len(lists)].append(SliderRow(c, self.on_change, show_tip=False))
            self.panel.append(cols)
        elif sid != "uniformity":
            self.panel.append(label("Nothing on this display can adjust this from software. Use the "
                                    "monitor's own menu while you look at the pattern.", "ct-tip", wrap=True))

        nav = Gtk.Box(spacing=8)
        hint = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        for key, what in (("←→", "steps"), ("H", "hide"), ("Esc", "close")):
            if self.single and key == "←→":
                continue
            hint.append(label(key, "ct-key"))
            hint.append(label(what, "ct-tip"))
        hint.set_hexpand(True)
        nav.append(hint)
        if not self.single:
            prev = Gtk.Button(label="Back")
            prev.set_sensitive(self.step > 0)
            prev.connect("clicked", lambda *_: self.go(-1))
            nav.append(prev)
        last = self.single or self.step == len(GUIDE) - 1
        nxt = Gtk.Button(label="Done" if last else "Next")
        nxt.add_css_class("suggested-action")
        nxt.connect("clicked", lambda *_: self.close() if last else self.go(1))
        nav.append(nxt)
        self.panel.append(nav)

    def set_message(self, text):
        if self.solid is not None:
            self.status.set_text(text)

    def go(self, delta):
        self.step = max(0, min(len(GUIDE) - 1, self.step + delta))
        self._build_panel()
        self.area.queue_draw()

    def _key(self, ctrl, keyval, code, state):
        if keyval == Gdk.KEY_Escape:
            self.close()
            return True
        if keyval in (Gdk.KEY_h, Gdk.KEY_H):
            self.panel.set_visible(not self.panel.get_visible())
            return True
        if self.solid is not None or keyval not in (Gdk.KEY_Left, Gdk.KEY_Right):
            return False
        if isinstance(self.get_focus(), Gtk.Range) and self.panel.get_visible():
            return False  # arrows belong to the focused slider
        delta = 1 if keyval == Gdk.KEY_Right else -1
        if GUIDE[self.step][4] == "solid":
            self.solid_idx = (self.solid_idx + delta) % len(SOLIDS)
            self.area.queue_draw()
        elif not self.single:
            self.go(delta)
        return True

    def _device_scale(self):
        surf = self.get_surface()
        try:
            return surf.get_scale() if surf else 1.0
        except AttributeError:
            return float(self.get_scale_factor())

    def _draw(self, area, cr, w, h):
        if self.solid is not None:
            cr.set_source_rgb(*self.solid)
            cr.paint()
            return
        kind = GUIDE[self.step][4]
        if kind == "solid":
            p_solid(cr, w, h, index=self.solid_idx)
        else:
            PATTERNS[kind](cr, w, h, self._device_scale(), True)
