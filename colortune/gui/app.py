"""ColorTune main window."""
import os
import shlex
import sys
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from .. import APP_ID, config, measure  # noqa: E402
from ..backends import WORKER, screenfilters  # noqa: E402
from ..colormath import cct  # noqa: E402
from ..displays import LUT_LABELS, discover  # noqa: E402
from . import theme  # noqa: E402
from .gamut import GamutDiagram, legend  # noqa: E402
from .patterns import GUIDE, PatternWindow, thumbnail  # noqa: E402
from .widgets import SliderRow, debounce, label  # noqa: E402

PAGES = [
    ("overview", "Overview", "video-display-symbolic"),
    ("adjust", "Adjust", "preferences-system-symbolic"),
    ("check", "Check", "view-grid-symbolic"),
    ("measure", "Measure", "find-location-symbolic"),
    ("profiles", "Profiles", "user-bookmarks-symbolic"),
]

SECTIONS = [
    ("tone", "Tone", ("black", "white", "gamma")),
    ("white_balance", "White balance", ("white_balance",)),
    ("colour", "Colour", ("saturation",)),
]


def launcher_command():
    pkg_parent = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return f"env PYTHONPATH={shlex.quote(pkg_parent)} {shlex.quote(sys.executable)} -m colortune"


def column():
    """Width-clamped column."""
    clamp = Adw.Clamp(maximum_size=880, tightening_threshold=600)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24, margin_top=24, margin_bottom=32,
                  margin_start=18, margin_end=18)
    clamp.set_child(box)
    return clamp, box


def page_box():
    """Scrollable, width-clamped column for a page."""
    scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
    clamp, box = column()
    scroller.set_child(clamp)
    return scroller, box


def heading(title, text=None):
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    box.append(label(title, "title-1"))
    if text:
        box.append(label(text, "dim-label", wrap=True))
    return box


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="ColorTune")
        self.set_default_size(1020, 760)
        self.set_size_request(360, 420)
        self.displays, self.current, self.controls = [], None, []
        self.sliders = []
        self.page = "overview"
        self.save_later = debounce(600, self.save_state)

        # sidebar
        self.nav = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.nav.add_css_class("navigation-sidebar")
        for key, title, icon in PAGES:
            row = Gtk.ListBoxRow()
            row.key = key
            b = Gtk.Box(spacing=12, margin_top=2, margin_bottom=2)
            b.append(Gtk.Image(icon_name=icon))
            b.append(label(title))
            row.set_child(b)
            self.nav.append(row)
        self.nav.connect("row-activated", lambda lb, row: self.show_page(row.key))
        self.display_dd = Gtk.DropDown.new_from_strings([])
        self.display_dd.set_margin_start(12)
        self.display_dd.set_margin_end(12)
        self.display_dd.set_visible(False)
        self.display_dd.connect("notify::selected", self._display_changed)
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        side.append(self.display_dd)
        side.append(self.nav)
        spacer = Gtk.Box(vexpand=True)
        side.append(spacer)
        side.append(label("Neutral grey on purpose: coloured interfaces bias how you see white.",
                          "ct-footnote", wrap=True, xalign=0))
        side.get_last_child().set_margin_start(18)
        side.get_last_child().set_margin_end(18)
        side.get_last_child().set_margin_bottom(14)
        side_tv = Adw.ToolbarView()
        side_header = Adw.HeaderBar()
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Detect displays again")
        refresh.connect("clicked", lambda *_: self.load())
        side_header.pack_start(refresh)
        side_tv.add_top_bar(side_header)
        side_tv.set_content(side)
        self.side_page = Adw.NavigationPage(title="ColorTune", child=side_tv)

        # content
        self.banner = Adw.Banner()
        self.banner.connect("button-clicked", self._pause_night_light)
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=150)
        loading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, valign=Gtk.Align.CENTER)
        spin = Adw.Spinner() if hasattr(Adw, "Spinner") else Gtk.Spinner(spinning=True)
        spin.set_size_request(32, 32)
        loading.append(spin)
        loading.append(label("Looking for displays and monitor controls…", "dim-label", xalign=0.5))
        self.stack.add_named(loading, "loading")
        content_tv = Adw.ToolbarView()
        content_tv.add_top_bar(Adw.HeaderBar())
        content_tv.add_top_bar(self.banner)
        content_tv.set_content(self.stack)
        self.content_page = Adw.NavigationPage(title="Overview", child=content_tv)

        self.split = Adw.NavigationSplitView(sidebar=self.side_page, content=self.content_page,
                                             min_sidebar_width=200, max_sidebar_width=240)
        self.toasts = Adw.ToastOverlay(child=self.split)
        self.set_content(self.toasts)

        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 660sp"))
        bp.add_setter(self.split, "collapsed", True)
        self.add_breakpoint(bp)
        self.narrow = bp

        GLib.timeout_add_seconds(60, self._refresh_banner)
        self.load()

    # ------------------------------------------------------------ data
    def load(self):
        self.stack.set_visible_child_name("loading")

        def work():
            env, displays = discover(state=config.load_state())
            GLib.idle_add(self._loaded, env, displays)
        threading.Thread(target=work, daemon=True).start()

    def _loaded(self, env, displays):
        self.env, self.displays = env, displays
        model = Gtk.StringList()
        for d in displays:
            model.append(d.name)
        self.display_dd.set_model(model)
        self.display_dd.set_visible(len(displays) > 1)
        self.current = displays[0] if displays else None
        self.rebuild()

    def _display_changed(self, dd, _):
        i = dd.get_selected()
        if 0 <= i < len(self.displays) and self.displays[i] is not self.current:
            self.current = self.displays[i]
            self.rebuild()

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=3))

    # ------------------------------------------------------------ pages
    def rebuild(self):
        for key, *_ in PAGES:
            old = self.stack.get_child_by_name(key)
            if old:
                self.stack.remove(old)
        self.sliders = []
        if self.current is None:
            sp = Adw.StatusPage(icon_name="video-display-symbolic", title="No displays found",
                                description="Connect a display and press refresh.")
            for key, *_ in PAGES:
                self.stack.add_named(sp if key == "overview" else Gtk.Box(), key)
        else:
            self.controls = self.current.controls()
            for key, *_ in PAGES:
                self.stack.add_named(getattr(self, f"build_{key}")(), key)
        self._refresh_banner()
        self.show_page(self.page)

    def show_page(self, key):
        self.page = key
        self.stack.set_visible_child_name(key)
        title = next(t for k, t, _ in PAGES if k == key)
        self.content_page.set_title(title)
        for row in self.nav:
            if getattr(row, "key", None) == key:
                self.nav.select_row(row)
        self.split.set_show_content(True)

    # ---- overview
    def build_overview(self):
        d = self.current
        scroller, box = page_box()
        hero = Gtk.Box(spacing=32)
        hero.add_css_class("ct-hero")
        prim = d.edid.get("primaries")
        if prim:
            left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, valign=Gtk.Align.CENTER)
            left.append(GamutDiagram(prim, 300))
            left.append(legend())
            hero.append(left)
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, valign=Gtk.Align.CENTER, hexpand=True)
        kind = "Built-in display" if d.internal else "External display"
        right.append(label(f"{kind} · {d.connector}".upper(), "ct-overline"))
        model = d.edid.get("name") or (f"{d.edid['vendor']} {d.edid['product']:04X}" if d.edid else d.connector)
        right.append(label(model, "ct-display-title", wrap=True))
        right.append(label(f"{self.env.session.capitalize()} · {self.env.desktop}", "dim-label"))
        stats = Gtk.Grid(column_spacing=28, row_spacing=16, margin_top=16)
        g = d.gamut()
        items = []
        if g:
            items += [(f"{g[0]:.0f}%", "of sRGB colours"), (f"{d.gamut_p3():.0f}%", "of Display P3")]
        if d.edid:
            items.append((f"{d.edid.get('gamma', 2.2):.2f}", "native gamma"))
            if prim:
                items.append((f"{round(cct(*prim['w']), -1):,.0f} K", "native white"))
        for i, (value, what) in enumerate(items):
            cell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            cell.append(label(value, "ct-stat"))
            cell.append(label(what, "ct-stat-label"))
            stats.attach(cell, i % 2, i // 2, 1, 1)
        right.append(stats)
        if prim:
            right.append(label("From the display's own report (EDID). A colorimeter gives true values.",
                               "ct-tip", wrap=True))
        hero.append(right)
        self.narrow.add_setter(hero, "orientation", Gtk.Orientation.VERTICAL)
        box.append(hero)

        if g and g[0] < 85:
            note = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            note.add_css_class("ct-note")
            note.append(label("Limited colour range", "heading"))
            note.append(label(
                f"This panel shows about {g[0]:.0f}% of the colours in sRGB, the standard most web and app "
                "content is made for, so vivid colours look muted. Software can't add colours the panel "
                "doesn't have. Calibration makes the colours it has correct and the blacks deep.",
                wrap=True))
            box.append(note)

        grp = Adw.PreferencesGroup(title="What can be adjusted on this display")
        for name, ok, detail in d.methods():
            row = Adw.ActionRow(title=name, subtitle=detail)
            img = Gtk.Image(icon_name="object-select-symbolic" if ok else "action-unavailable-symbolic")
            img.add_css_class("ct-ok" if ok else "ct-no")
            row.add_prefix(img)
            grp.add(row)
        box.append(grp)

        start = Adw.PreferencesGroup(title="Start")
        for title, sub, fn in (
                ("Guided calibration", "Six full-screen steps, from black level to saturation",
                 lambda: self.open_patterns("black")),
                ("Adjust", "Sliders for everything this display supports", lambda: self.show_page("adjust")),
                ("Check saturation", "Colour bars, gradients and skin tones", lambda: self.open_patterns("saturation", single=True))):
            row = Adw.ActionRow(title=title, subtitle=sub, activatable=True)
            row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
            row.connect("activated", lambda _r, f=fn: f())
            start.add(row)
        box.append(start)
        return scroller

    # ---- adjust
    def build_adjust(self):
        d = self.current
        scroller, box = page_box()
        box.append(heading("Adjust", "Start with the display hardware, then fine-tune the picture. Every change "
                                     "applies immediately and is remembered."))
        if not self.controls:
            box.append(Adw.StatusPage(icon_name="action-unavailable-symbolic", title="Nothing to adjust from software",
                                      description="Use the monitor's own buttons. ColorTune's Check patterns "
                                                  "still help you set it up."))
            return scroller
        hw = [c for c in self.controls if c.group == "hardware"]
        if hw:
            grp = Adw.PreferencesGroup(title="Display hardware",
                                       description="Changes the screen itself. This gives the best quality, so start here.")
            for c in hw:
                grp.add(self._slider(c))
            box.append(grp)
        pic = [c for c in self.controls if c.group == "picture"]
        for key, title, steps in SECTIONS:
            cs = [c for c in pic if set(c.steps) & set(steps)]
            if not cs:
                continue
            src = cs[0].source
            desc = {"ada": "Through Ada's colour filter"}.get(src, LUT_LABELS.get(src, ""))
            grp = Adw.PreferencesGroup(title=title, description=desc)
            reset = Gtk.Button(label="Reset", valign=Gtk.Align.CENTER)
            reset.add_css_class("flat")
            reset.connect("clicked", lambda _b, group=cs: self._reset(group))
            grp.set_header_suffix(reset)
            for c in cs:
                grp.add(self._slider(c))
            box.append(grp)
        self.error = label("", "error", wrap=True)
        box.append(self.error)
        return scroller

    def _slider(self, c):
        s = SliderRow(c, self._changed)
        self.sliders.append(s)
        return s

    def _reset(self, controls):
        for c in controls:
            if c.default is not None:
                for s in self.sliders:
                    if s.control is c:
                        s.scale.set_value(c.default)

    # ---- check
    def build_check(self):
        scroller, box = page_box()
        top = Gtk.Box(spacing=16)
        h = heading("Check", "Full-screen test patterns. Press a pattern to check one thing, or run "
                             "the guided calibration to go through them all with the right sliders "
                             "beside each one.")
        h.set_hexpand(True)
        top.append(h)
        go = Gtk.Button(label="Guided calibration", valign=Gtk.Align.CENTER)
        go.add_css_class("suggested-action")
        go.add_css_class("pill")
        go.connect("clicked", lambda *_: self.open_patterns("black"))
        top.append(go)
        self.narrow.add_setter(top, "orientation", Gtk.Orientation.VERTICAL)
        box.append(top)
        flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=3, min_children_per_line=1,
                           column_spacing=14, row_spacing=14, homogeneous=True)
        for sid, title, short, _text, kind in GUIDE:
            card = Gtk.Button()
            card.add_css_class("flat")
            card.add_css_class("ct-card")
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            inner.append(thumbnail(kind))
            inner.append(label(title, "ct-card-title"))
            inner.append(label(short, "ct-tip", wrap=True))
            card.set_child(inner)
            card.connect("clicked", lambda _b, s=sid: self.open_patterns(s, single=True))
            flow.append(card)
        box.append(flow)
        return scroller

    # ---- measure
    def build_measure(self):
        if not measure.available():
            sp = Adw.StatusPage(icon_name="find-location-symbolic", title="Connect a colorimeter",
                                description="A colorimeter is a small sensor that sits on the screen and "
                                            "measures its light. It's the only way to know the colours are "
                                            "right, rather than looking right to tired eyes.\n\n"
                                            "Install ArgyllCMS (the installer does this) and plug in a "
                                            "supported meter, such as a Calibrite Display Plus HL, "
                                            "Display Pro or SpyderX.")
            return sp
        return MeasurePage(self)

    # ---- profiles
    def build_profiles(self):
        scroller, box = page_box()
        box.append(heading("Profiles", "Save the current settings of every display under a name, "
                                       "for example Movies, Night or Photo editing, and switch between them."))
        grp = Adw.PreferencesGroup(title="Saved")
        profiles = config.load_profiles()
        if not profiles:
            grp.add(Adw.ActionRow(title="No profiles yet", subtitle="Save one below"))
        for name in sorted(profiles):
            n = len(profiles[name])
            row = Adw.ActionRow(title=name, subtitle=f"{n} display{'s' if n != 1 else ''}")
            load = Gtk.Button(label="Apply", valign=Gtk.Align.CENTER)
            load.connect("clicked", lambda _b, nm=name: self.load_profile(nm))
            delete = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Delete")
            delete.add_css_class("flat")
            delete.connect("clicked", lambda _b, nm=name: self.delete_profile(nm))
            row.add_suffix(load)
            row.add_suffix(delete)
            grp.add(row)
        box.append(grp)
        save = Adw.PreferencesGroup()
        entry = Adw.EntryRow(title="Save current settings as…", show_apply_button=True)
        entry.connect("apply", lambda e: e.get_text().strip() and self.save_profile(e.get_text().strip()))
        save.add(entry)
        box.append(save)
        startup = Adw.PreferencesGroup(title="Startup")
        sw = Adw.SwitchRow(title="Apply at login", subtitle="Restore these settings every time you sign in",
                           active=config.autostart_enabled())
        sw.connect("notify::active", lambda s, _: config.set_autostart(s.get_active(), launcher_command()))
        startup.add(sw)
        box.append(startup)
        return scroller

    # ------------------------------------------------------------ actions
    def _changed(self, control):
        self.save_later()
        GLib.timeout_add(800, self._show_error)

    def _show_error(self):
        err = self.current.last_error if self.current else None
        if getattr(self, "error", None):
            self.error.set_text(err or "")
        return False

    def _refresh_banner(self):
        nl = self.env.refresh_night_light() if getattr(self, "env", None) else None
        if nl:
            self.banner.set_title(f"{nl['detail']}. Colours look warmer than they are, so pause it "
                                  "before you calibrate.")
            self.banner.set_button_label("Pause" if nl["can_pause"] else None)
        self.banner.set_revealed(bool(nl))
        return True

    def _pause_night_light(self, *_):
        if screenfilters.pause_night_light():
            self.toast("Night Light paused until its next scheduled change")
        GLib.timeout_add(400, lambda: (self._refresh_banner(), False)[1])

    def open_patterns(self, start="black", single=False):
        win = PatternWindow(self.get_application(), self.controls, self._changed, start=start, single=single)
        win.connect("close-request", lambda *_: (self._resync(), False)[1])
        win.present()

    def _resync(self):
        for s in self.sliders:
            s.sync()
        self._refresh_banner()

    def save_state(self):
        state = config.load_state()
        for d in self.displays:
            state["displays"][d.key] = d.to_state()
        config.save_state(state)

    def save_profile(self, name):
        self.save_state()
        profiles = config.load_profiles()
        profiles[name] = config.load_state()["displays"]
        config.save_profiles(profiles)
        self.toast(f"Saved “{name}”")
        self.rebuild()

    def delete_profile(self, name):
        profiles = config.load_profiles()
        profiles.pop(name, None)
        config.save_profiles(profiles)
        self.toast(f"Deleted “{name}”")
        self.rebuild()

    def load_profile(self, name):
        prof = config.load_profiles().get(name, {})
        for d in self.displays:
            if d.key in prof:
                d.load(prof[d.key], from_profile=True)
                d.apply_all()
        self.save_state()
        self.toast(f"Applied “{name}”")
        self.rebuild()


class MeasurePage(Gtk.ScrolledWindow):
    """Colorimeter readings and automatic white balance via ArgyllCMS."""

    def __init__(self, main):
        super().__init__(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        self.main = main
        self.d = main.current
        self.white = self.black = None
        clamp, box = column()
        self.set_child(clamp)
        box.append(heading("Measure", "Let the screen warm up for about 20 minutes. When a patch appears, "
                                      "lay the sensor flat in its centre."))
        tiles = Gtk.Box(spacing=12, homogeneous=True)
        self.tiles = {}
        for key, what in (("white", "White · cd/m²"), ("black", "Black · cd/m²"),
                          ("contrast", "Contrast"), ("cct", "White point")):
            t = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            t.add_css_class("ct-hero")
            v = label("—", "ct-measure-value")
            t.append(v)
            t.append(label(what, "ct-stat-label"))
            self.tiles[key] = v
            tiles.append(t)
        main.narrow.add_setter(tiles, "orientation", Gtk.Orientation.VERTICAL)
        box.append(tiles)
        self.detail = label("", "dim-label", wrap=True)
        box.append(self.detail)
        grp = Adw.PreferencesGroup(title="Readings")
        for title, sub, fn in (("Measure white", "Brightness and colour temperature", self.m_white),
                               ("Measure black", "How deep the blacks are, and the contrast ratio", self.m_black),
                               ("Auto white balance", "Adjusts the RGB gains until white is D65 (6504 K)",
                                self.auto_wb)):
            row = Adw.ActionRow(title=title, subtitle=sub, activatable=True)
            row.add_suffix(Gtk.Image(icon_name="media-playback-start-symbolic"))
            row.connect("activated", lambda _r, f=fn: f())
            grp.add(row)
        box.append(grp)

    def _patch(self, rgb, msg):
        win = PatternWindow(self.main.get_application(), [], solid=rgb, message=msg)
        win.present()
        return win

    def _run(self, rgb, msg, then):
        win = self._patch(rgb, msg)

        def work():
            time.sleep(1.5)
            try:
                xyz = measure.read()
                GLib.idle_add(then, xyz, win)
            except Exception as e:
                GLib.idle_add(self._fail, str(e), win)
        threading.Thread(target=work, daemon=True).start()

    def _fail(self, err, win):
        win.close()
        self.main.toast(f"Measurement failed: {err}")

    def _report(self):
        if self.white:
            w = measure.describe(self.white)
            self.tiles["white"].set_text(f"{w['Y']:.0f}")
            self.tiles["cct"].set_text(f"{w['cct']:,.0f} K" if w["cct"] else "—")
            self.detail.set_text(f"White x {w['x']:.4f}, y {w['y']:.4f}. Distance from D65: "
                                 f"Δxy {w['dxy']:.4f} ({'excellent' if w['dxy'] < 0.003 else 'good' if w['dxy'] < 0.006 else 'needs balancing'}).")
        if self.black:
            self.tiles["black"].set_text(f"{self.black[1]:.3f}")
        if self.white and self.black and self.black[1] > 0:
            self.tiles["contrast"].set_text(f"{self.white[1] / self.black[1]:,.0f}:1")

    def m_white(self):
        def done(xyz, win):
            win.close()
            self.white = xyz
            self._report()
        self._run((1, 1, 1), "Measuring white…", done)

    def m_black(self):
        def done(xyz, win):
            win.close()
            self.black = xyz
            self._report()
        self._run((0, 0, 0), "Measuring black…", done)

    def _gain_controls(self):
        cs = {c.key: c for c in self.main.controls}
        hw = [cs.get(k) for k in ("vcp16", "vcp18", "vcp1A")]
        if all(hw):
            return hw, True
        sw = [cs.get(k) for k in ("red", "green", "blue")]
        if all(sw):
            return sw, False
        return None, False

    def auto_wb(self):
        controls, linear = self._gain_controls()
        if not controls:
            self.main.toast("This display has no RGB gain control to adjust")
            return
        win = self._patch((1, 1, 1), "Auto white balance. Keep the sensor in place…")
        prim = self.d.edid.get("primaries")

        def work():
            try:
                xyz = None
                for i in range(6):
                    time.sleep(1.5)
                    xyz = measure.read()
                    info = measure.describe(xyz)
                    GLib.idle_add(win.set_message, f"Pass {i + 1}: {info['cct']:.0f} K, Δxy {info['dxy']:.4f}")
                    if info["dxy"] < 0.002:
                        break
                    gains = [c.value / c.maximum for c in controls]
                    new = measure.balance(xyz, gains, linear=linear, primaries=prim)
                    for c, g in zip(controls, new):
                        c.value = round(g * c.maximum / c.step) * c.step
                        c.setter(c.value)
                    WORKER.flush()
                self.white = xyz
                GLib.idle_add(self._wb_done, win)
            except Exception as e:
                GLib.idle_add(self._fail, str(e), win)
        threading.Thread(target=work, daemon=True).start()

    def _wb_done(self, win):
        win.close()
        self.main._resync()
        self.main.save_state()
        self._report()
        self.main.toast("White balance done")


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_activate(self):
        win = self.get_active_window()
        if not win:
            theme.install()
            win = MainWindow(self)
        win.present()


def main():
    return App().run([sys.argv[0]])
