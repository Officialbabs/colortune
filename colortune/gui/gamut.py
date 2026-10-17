"""CIE 1931 xy chromaticity diagram: the panel's colour range against sRGB and P3."""
import cairo
import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from ..colormath import DISPLAY_P3, SPECTRAL_LOCUS, SRGB, xy_to_srgb  # noqa: E402

X_RANGE = (0.0, 0.8)
Y_RANGE = (0.0, 0.9)
_FILL = None


def _fill_surface(n=220):
    """Chromaticity colours as an image (computed once)."""
    global _FILL
    if _FILL is not None:
        return _FILL
    surf = cairo.ImageSurface(cairo.FORMAT_RGB24, n, n)
    stride = surf.get_stride()
    buf = surf.get_data()
    for j in range(n):
        y = Y_RANGE[1] - (j + 0.5) / n * (Y_RANGE[1] - Y_RANGE[0])
        row = j * stride
        for i in range(n):
            x = X_RANGE[0] + (i + 0.5) / n * (X_RANGE[1] - X_RANGE[0])
            if y <= 0.002:
                continue
            r, g, b = xy_to_srgb(x, y)
            o = row + i * 4
            buf[o] = int(b * 255)
            buf[o + 1] = int(g * 255)
            buf[o + 2] = int(r * 255)
    surf.mark_dirty()
    _FILL = surf
    return surf


class GamutDiagram(Gtk.DrawingArea):
    def __init__(self, primaries, size=300):
        super().__init__()
        self.primaries = primaries
        self.set_content_width(size)
        self.set_content_height(size)
        self.set_draw_func(self._draw)

    def _draw(self, area, cr, w, h):
        pad = 26
        side = min(w, h) - pad - 6
        ox, oy = pad, 6

        def pt(x, y):
            return (ox + (x - X_RANGE[0]) / (X_RANGE[1] - X_RANGE[0]) * side,
                    oy + (Y_RANGE[1] - y) / (Y_RANGE[1] - Y_RANGE[0]) * side)

        # grid
        cr.set_line_width(1)
        cr.set_source_rgba(1, 1, 1, 0.06)
        for v in range(0, 9):
            x0, y0 = pt(v / 10, Y_RANGE[0])
            x1, y1 = pt(v / 10, Y_RANGE[1])
            cr.move_to(round(x0) + .5, y0)
            cr.line_to(round(x1) + .5, y1)
            ya, yb = pt(X_RANGE[0], v / 10), pt(X_RANGE[1], v / 10)
            cr.move_to(ya[0], round(ya[1]) + .5)
            cr.line_to(yb[0], round(yb[1]) + .5)
        cr.stroke()
        cr.set_source_rgba(1, 1, 1, 0.35)
        cr.select_font_face("Sans")
        cr.set_font_size(9)
        for v in (0, 2, 4, 6, 8):
            x, y = pt(v / 10, 0)
            cr.move_to(x - 6, y + 14)
            cr.show_text(f"0.{v}" if v else "0")
            x, y = pt(0, v / 10)
            if v:
                cr.move_to(x - 22, y + 3)
                cr.show_text(f"0.{v}")

        # spectral locus, filled with (dimmed) colour
        cr.save()
        for i, (x, y) in enumerate(SPECTRAL_LOCUS):
            cr.line_to(*pt(x, y)) if i else cr.move_to(*pt(x, y))
        cr.close_path()
        path = cr.copy_path()
        cr.clip()
        fill = _fill_surface()
        cr.translate(ox, oy)
        cr.scale(side / fill.get_width(), side / fill.get_height())
        cr.set_source_surface(fill, 0, 0)
        cr.get_source().set_filter(cairo.FILTER_BILINEAR)
        cr.paint_with_alpha(0.55)
        cr.restore()
        cr.append_path(path)
        cr.set_source_rgba(1, 1, 1, 0.25)
        cr.set_line_width(1)
        cr.stroke()

        def triangle(prim):
            for i, c in enumerate("rgb"):
                cr.line_to(*pt(*prim[c])) if i else cr.move_to(*pt(*prim[c]))
            cr.close_path()

        def halo_stroke(width, alpha, dash=None):
            path = cr.copy_path()
            cr.set_dash([])
            cr.set_source_rgba(0, 0, 0, 0.55)
            cr.set_line_width(width + 2.2)
            cr.stroke()
            cr.append_path(path)
            cr.set_dash(dash or [])
            cr.set_source_rgba(1, 1, 1, alpha)
            cr.set_line_width(width)
            cr.stroke()
            cr.set_dash([])

        triangle(DISPLAY_P3)
        halo_stroke(1.2, 0.7, [4, 3])
        triangle(SRGB)
        halo_stroke(1.3, 0.75)
        if self.primaries:
            triangle(self.primaries)
            cr.set_source_rgba(1, 1, 1, 0.10)
            cr.fill_preserve()
            halo_stroke(2.4, 1.0)
            for c in "rgb":
                x, y = pt(*self.primaries[c])
                cr.set_source_rgba(0, 0, 0, 0.6)
                cr.arc(x, y, 4.6, 0, 6.2832)
                cr.fill()
                cr.set_source_rgba(1, 1, 1, 1)
                cr.arc(x, y, 3.4, 0, 6.2832)
                cr.fill()
        # D65 reference
        dx, dy = pt(0.3127, 0.3290)
        cr.arc(dx, dy, 5, 0, 6.2832)
        halo_stroke(1.2, 0.95)


def legend():
    """Small key matching the diagram's line styles."""
    box = Gtk.Box(spacing=14, halign=Gtk.Align.CENTER)
    for text, style in (("This display", "panel"), ("sRGB", "srgb"), ("Display P3", "p3"), ("D65 white", "d65")):
        sw = Gtk.DrawingArea(content_width=22, content_height=10)

        def draw(_a, cr, w, h, s=style):
            if s == "d65":
                cr.set_source_rgba(1, 1, 1, .9)
                cr.set_line_width(1.2)
                cr.arc(w / 2, h / 2, 4, 0, 6.2832)
                cr.stroke()
                return
            cr.move_to(1, h / 2)
            cr.line_to(w - 1, h / 2)
            if s == "panel":
                cr.set_source_rgba(1, 1, 1, 1)
                cr.set_line_width(2.4)
            else:
                cr.set_source_rgba(1, 1, 1, .6)
                cr.set_line_width(1.4)
                if s == "p3":
                    cr.set_dash([4, 3])
            cr.stroke()
        sw.set_draw_func(draw)
        item = Gtk.Box(spacing=5)
        item.append(sw)
        lab = Gtk.Label(label=text)
        lab.add_css_class("ct-tip")
        item.append(lab)
        box.append(item)
    return box
