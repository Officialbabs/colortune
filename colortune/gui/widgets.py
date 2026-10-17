"""Shared widgets."""
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from .theme import track_class  # noqa: E402


def label(text, *classes, xalign=0.0, wrap=False, selectable=False):
    lab = Gtk.Label(label=text, xalign=xalign)
    lab.set_wrap(wrap)
    if wrap:
        lab.set_natural_wrap_mode(Gtk.NaturalWrapMode.WORD) if hasattr(lab, "set_natural_wrap_mode") else None
    lab.set_selectable(selectable)
    for c in classes:
        lab.add_css_class(c)
    return lab


def fmt(control, v):
    if control.key == "backlight":
        return f"{v:.0f}%"
    if control.key == "black":
        return f"{v:+.3f}".replace("-", "−") if abs(v) > 1e-9 else "0"
    if control.digits == 0:
        return f"{v:.0f}"
    return f"{v:.{control.digits}f}"


class SliderRow(Adw.PreferencesRow):
    """One Control: name, value, reset-when-changed, a track that shows what it does."""

    def __init__(self, control, on_change=None, show_tip=True):
        super().__init__(activatable=False, focusable=False)
        self.control = control
        self.on_change = on_change
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.add_css_class("ct-slider-row")
        top = Gtk.Box(spacing=8)
        name = label(control.label)
        name.set_hexpand(True)
        top.append(name)
        self.reset = Gtk.Button(icon_name="edit-undo-symbolic", tooltip_text="Back to neutral",
                                valign=Gtk.Align.CENTER)
        self.reset.add_css_class("flat")
        self.reset.add_css_class("circular")
        self.reset.connect("clicked", lambda *_: self.scale.set_value(control.default))
        top.append(self.reset)
        self.value = label("", "ct-value", xalign=1.0)
        top.append(self.value)
        box.append(top)

        self.scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, control.minimum, control.maximum,
                                              control.step)
        self.scale.set_draw_value(False)
        self.scale.add_css_class("ct-track")
        self.scale.add_css_class(track_class(control.key))
        self.scale.update_property([Gtk.AccessibleProperty.LABEL], [control.label])
        if control.default is not None and control.minimum < control.default < control.maximum:
            self.scale.add_mark(control.default, Gtk.PositionType.BOTTOM, None)
        self.scale.set_value(control.value)
        self.scale.connect("value-changed", self._changed)
        box.append(self.scale)
        if show_tip and control.tip:
            box.append(label(control.tip, "ct-tip", wrap=True))
        self.set_child(box)
        self._show(control.value)

    def _show(self, v):
        self.value.set_text(fmt(self.control, v))
        d = self.control.default
        self.reset.set_visible(d is not None and abs(v - d) > self.control.step / 2)

    def _changed(self, scale):
        c = self.control
        v = round(round(scale.get_value() / c.step) * c.step, max(c.digits, 0) + 3)
        if v == c.value:
            return
        c.value = v
        self._show(v)
        c.setter(v)
        if self.on_change:
            self.on_change(c)

    def sync(self):
        self.scale.set_value(self.control.value)
        self._show(self.control.value)


def debounce(ms, fn):
    """Return a callable that runs fn once, ms after the last call."""
    state = {"id": 0}

    def fire():
        state["id"] = 0
        fn()
        return False

    def call(*_):
        if state["id"]:
            GLib.source_remove(state["id"])
        state["id"] = GLib.timeout_add(ms, fire)
    return call
