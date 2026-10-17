"""ColorTune main window."""
import os
import shlex
import sys
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk, Pango  # noqa: E402

from .. import APP_ID, config, measure  # noqa: E402
from ..backends import WORKER, ddc, screenfilters  # noqa: E402
from ..colormath import cct  # noqa: E402
from ..displays import LUT_LABELS, apply_desktop_saturation, discover  # noqa: E402
from . import theme  # noqa: E402
from .gamut import GamutDiagram, legend  # noqa: E402
from .patterns import GUIDE, PatternWindow, monitor_for, thumbnail  # noqa: E402
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

PROFILE_MODES = [("curves", "Curves only", "Colours stay as they are; only brightness and white balance change"),
                 ("edid", "Display's reported colours",
                  "Uses the colour data the display reports about itself (EDID); falls back to sRGB if it looks wrong")]


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


def intro(text):
    return label(text, "dim-label", wrap=True)


def pill(text, primary=False, cb=None):
    b = Gtk.Button(label=text, valign=Gtk.Align.CENTER)
    b.add_css_class("pill")
    if primary:
        b.add_css_class("suggested-action")
    if cb:
        b.connect("clicked", lambda *_: cb())
    return b


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="ColorTune")
        self.set_default_size(1020, 760)
        self.set_size_request(360, 420)
        self.env = None
        self.displays, self.current, self.controls = [], None, []
        self.sliders = []
        self.page = "overview"
        self.night_token = None
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
        self.display_dd = Gtk.DropDown(model=Gtk.StringList(), factory=self._dd_factory(True),
                                       list_factory=self._dd_factory(False))
        self.display_dd.set_margin_start(12)
        self.display_dd.set_margin_end(12)
        self.display_dd.set_visible(False)
        self._dd_handler = self.display_dd.connect("notify::selected", self._display_changed)
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        side.append(self.display_dd)
        side.append(self.nav)
        side.append(Gtk.Box(vexpand=True))
        foot = label("Neutral grey on purpose: coloured interfaces bias how you see white.",
                     "ct-footnote", wrap=True)
        foot.set_margin_start(18)
        foot.set_margin_end(18)
        foot.set_margin_bottom(14)
        side.append(foot)
        side_tv = Adw.ToolbarView()
        side_header = Adw.HeaderBar()
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Detect displays again")
        refresh.connect("clicked", lambda *_: self.load())
        side_header.pack_start(refresh)
        brand = Gtk.Box(spacing=8)
        brand.append(Gtk.Image(icon_name=APP_ID, pixel_size=22))
        brand.append(label("ColorTune", "heading"))
        side_header.set_title_widget(brand)
        side_tv.add_top_bar(side_header)
        side_tv.set_content(side)
        self.side_page = Adw.NavigationPage(title="ColorTune", child=side_tv)

        # content
        self.banner = Adw.Banner()
        self.banner.connect("button-clicked", lambda *_: self.pause_night_light())
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=150,
                               hhomogeneous=False, vhomogeneous=False)
        loading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, valign=Gtk.Align.CENTER)
        spin = Adw.Spinner() if hasattr(Adw, "Spinner") else Gtk.Spinner(spinning=True)
        spin.set_size_request(32, 32)
        loading.append(spin)
        loading.append(label("Looking for displays and monitor controls…", "dim-label", xalign=0.5))
        self.stack.add_named(loading, "loading")
        content_tv = Adw.ToolbarView()
        self.content_header = Adw.HeaderBar(show_title=False)
        content_tv.add_top_bar(self.content_header)
        content_tv.add_top_bar(self.banner)
        content_tv.set_content(self.stack)
        self.content_page = Adw.NavigationPage(title="Overview", child=content_tv)

        self.split = Adw.NavigationSplitView(sidebar=self.side_page, content=self.content_page,
                                             min_sidebar_width=200, max_sidebar_width=240)
        self.toasts = Adw.ToastOverlay(child=self.split)
        self.set_content(self.toasts)

        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 660sp"))
        narrow.add_setter(self.split, "collapsed", True)
        narrow.add_setter(self.content_header, "show-title", True)
        self.add_breakpoint(narrow)

        self.connect("close-request", self._on_close)
        GLib.timeout_add_seconds(60, lambda: (self.refresh_banner(), True)[1])
        self.load()

    @staticmethod
    def _dd_factory(ellipsize):
        """Long display names: ellipsized on the button, in full in the popup."""
        f = Gtk.SignalListItemFactory()
        f.connect("setup", lambda _f, item: item.set_child(Gtk.Label(
            xalign=0, ellipsize=Pango.EllipsizeMode.END if ellipsize else Pango.EllipsizeMode.NONE)))
        f.connect("bind", lambda _f, item: item.get_child().set_text(item.get_item().get_string()))
        return f

    def new_page(self):
        """A page container with its own breakpoints (by page width), so layout setters
        live and die with the page. libadwaita applies only the last matching
        breakpoint, so "narrow" repeats everything "medium" does."""
        bin_ = Adw.BreakpointBin(width_request=300, height_request=200)
        self._bp_medium = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 700sp"))
        self._bp_narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 560sp"))
        bin_.add_breakpoint(self._bp_medium)
        bin_.add_breakpoint(self._bp_narrow)
        return bin_

    def page_box(self):
        bin_ = self.new_page()
        scroller, box = page_box()
        bin_.set_child(scroller)
        return bin_, box

    def responsive(self, widget, prop, medium=None, narrow=None):
        """Breakpoint setters for a widget on the page being built."""
        if medium is not None:
            self._bp_medium.add_setter(widget, prop, medium)
        self._bp_narrow.add_setter(widget, prop, narrow if narrow is not None else medium)

    # ------------------------------------------------------------ data
    def load(self):
        self.stack.set_visible_child_name("loading")

        def work():
            env, displays = discover(state=config.load_state())
            GLib.idle_add(self._loaded, env, displays)
        threading.Thread(target=work, daemon=True).start()

    def _loaded(self, env, displays):
        keep = self.current.key if self.current else None
        self.env, self.displays = env, displays
        self.current = next((d for d in displays if d.key == keep), displays[0] if displays else None)
        model = Gtk.StringList()
        for d in displays:
            model.append(d.name)
        self.display_dd.handler_block(self._dd_handler)
        self.display_dd.set_model(model)
        if self.current:
            self.display_dd.set_selected(displays.index(self.current))
        self.display_dd.handler_unblock(self._dd_handler)
        self.display_dd.set_visible(len(displays) > 1)
        self.rebuild()

    def _display_changed(self, dd, _):
        i = dd.get_selected()
        if 0 <= i < len(self.displays) and self.displays[i] is not self.current:
            self.current = self.displays[i]
            self.rebuild()

    def toast(self, text, button=None, action=None, timeout=3):
        t = Adw.Toast(title=text, timeout=timeout)
        if button:
            t.set_button_label(button)
            t.connect("button-clicked", lambda *_: action())
        self.toasts.add_toast(t)

    # ------------------------------------------------------------ pages
    def rebuild(self):
        for key, *_ in PAGES:
            old = self.stack.get_child_by_name(key)
            if old:
                self.stack.remove(old)
        self.sliders = []
        self.sat_stat = None
        if self.current is None:
            for key, *_ in PAGES:
                self.stack.add_named(Adw.StatusPage(icon_name="video-display-symbolic", title="No displays found",
                                                    description="Connect a display and press refresh."), key)
        else:
            self.controls = self.current.controls()
            for key, *_ in PAGES:
                self.stack.add_named(getattr(self, f"build_{key}")(), key)
        self.refresh_banner()
        self.show_page(self.page)

    def show_page(self, key):
        self.page = key
        self.stack.set_visible_child_name(key)
        self.content_page.set_title(next(t for k, t, _ in PAGES if k == key))
        for row in self.nav:
            if getattr(row, "key", None) == key:
                self.nav.select_row(row)
        self.split.set_show_content(True)

    # ---- overview
    def build_overview(self):
        d = self.current
        scroller, box = self.page_box()
        hero = Gtk.Box(spacing=32)
        hero.add_css_class("ct-hero")
        prim = d.edid.get("primaries")
        if prim:
            left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, valign=Gtk.Align.CENTER,
                           halign=Gtk.Align.CENTER)
            diagram = GamutDiagram(prim, 300)
            left.append(diagram)
            left.append(legend())
            hero.append(left)
            self.responsive(diagram, "content-width", narrow=240)
            self.responsive(diagram, "content-height", narrow=240)
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, valign=Gtk.Align.CENTER, hexpand=True)
        kind = "Built-in display" if d.internal else "External display"
        parts = [kind]
        if d.edid and not d.edid.get("name"):
            parts.append(f"{d.edid['vendor']} {d.edid['product']:04X}")
        parts.append(d.connector)
        right.append(label(" · ".join(parts), "ct-overline"))
        title = d.edid.get("name") or ("Built-in screen" if d.internal else "External monitor")
        right.append(label(title, "ct-display-title", wrap=True))
        stats = Gtk.Grid(column_spacing=28, row_spacing=16, margin_top=14)
        g = d.gamut()
        items = []
        if g:
            items += [(f"{g[0]:.0f}%", "of sRGB colours"), (f"{d.gamut_p3():.0f}%", "of Display P3")]
        if prim:
            items.append((f"{round(cct(*prim['w']), -1):,.0f} K", "native white"))
        sat = next((c for c in self.controls if c.key == "saturation"), None)
        if sat:
            self.sat_stat = label(f"{sat.value * 100:.0f}%", "ct-stat")
            items.append((self.sat_stat, "saturation now"))
        for i, (value, what) in enumerate(items):
            cell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            cell.append(value if isinstance(value, Gtk.Widget) else label(value, "ct-stat"))
            cell.append(label(what, "ct-stat-label"))
            stats.attach(cell, i % 2, i // 2, 1, 1)
        right.append(stats)
        if prim:
            right.append(label("Colour range as the display reports it (EDID), not measured.",
                               "ct-tip", wrap=True))
        if hasattr(Adw, "WrapBox"):  # libadwaita 1.7+: the pills wrap when space runs out
            actions = Adw.WrapBox(child_spacing=8, line_spacing=8, margin_top=14)
        else:
            actions = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=14,
                              halign=Gtk.Align.START)
        actions.append(pill("Start guided calibration", True, lambda: self.open_patterns("black")))
        actions.append(pill("Check saturation", False, lambda: self.open_patterns("saturation", single=True)))
        right.append(actions)
        hero.append(right)
        self.responsive(hero, "orientation", Gtk.Orientation.VERTICAL)
        box.append(hero)

        problem = d.edid_problem() if prim else None
        if problem:
            box.append(self._note("This display's colour data looks wrong",
                                  f"It reports colours that can't be right ({problem}), so the chart above "
                                  "is unreliable and ColorTune won't use it for colour profiles."))
        elif g and g[0] < 85:
            box.append(self._note(
                "Limited colour range",
                f"This panel shows about {g[0]:.0f}% of the colours in sRGB, the standard most web and app content "
                "is made for, so vivid colours look muted. Software can't add colours the panel doesn't have. "
                "Calibration makes the colours it has correct and the blacks deep."))

        grp = Adw.PreferencesGroup(title="What ColorTune can change here",
                                   description=f"{self.env.desktop} · {self.env.session.capitalize()}")
        for name, ok, detail in d.methods():
            row = Adw.ActionRow(title=name, subtitle=detail)
            img = Gtk.Image(icon_name="object-select-symbolic" if ok else "action-unavailable-symbolic")
            img.add_css_class("ct-ok" if ok else "ct-no")
            row.add_prefix(img)
            grp.add(row)
        box.append(grp)
        return scroller

    def _note(self, title, text):
        note = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        note.add_css_class("ct-note")
        note.append(label(title, "heading"))
        note.append(label(text, wrap=True))
        return note

    # ---- adjust
    def build_adjust(self):
        d = self.current
        scroller, box = self.page_box()
        box.append(intro("Start with the display hardware, then fine-tune the picture. Changes apply "
                         "immediately and are remembered."))
        hw = [c for c in self.controls if c.group == "hardware"]
        if hw or d.ddc_preset is not None:
            grp = Adw.PreferencesGroup(title="Display hardware",
                                       description="Changes the screen itself. This gives the best quality, so start here.")
            if d.ddc_preset is not None:
                grp.add(self._preset_row(d))
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
            suffix = Gtk.Box(spacing=4)
            test = Gtk.Button(child=Adw.ButtonContent(icon_name="view-grid-symbolic", label="Test pattern"),
                              valign=Gtk.Align.CENTER)
            test.add_css_class("flat")
            test.connect("clicked", lambda _b, s=steps[0]: self.open_patterns(s, single=True))
            suffix.append(test)
            if len(cs) > 1:
                reset = Gtk.Button(label="Reset", valign=Gtk.Align.CENTER)
                reset.add_css_class("flat")
                reset.connect("clicked", lambda _b, group=cs: self._reset(group))
                suffix.append(reset)
            grp.set_header_suffix(suffix)
            for c in cs:
                grp.add(self._slider(c))
            box.append(grp)
        if d.lut == "icc":
            box.append(self._profile_mode_group(d))
        missing = [(n, detail) for n, ok, detail in d.methods() if not ok]
        if missing:
            grp = Adw.PreferencesGroup(title="Not available on this display")
            for name, detail in missing:
                if name == "Picture curves":
                    name = "Black point, gamma and white balance"
                    detail = f"{detail[0].upper()}{detail[1:]}. The Check patterns still show whether they need fixing."
                else:
                    detail = detail[0].upper() + detail[1:]
                grp.add(Adw.ActionRow(title=name, subtitle=detail, sensitive=False))
            box.append(grp)
        self.error = label("", "error", wrap=True)
        box.append(self.error)
        return scroller

    def _preset_row(self, d):
        codes = list(ddc.PRESETS)
        names = [ddc.PRESETS[c] for c in codes]
        if d.ddc_preset not in codes:
            codes.append(d.ddc_preset)
            names.append(f"Other ({d.ddc_preset:#04x})")
        row = Adw.ComboRow(title="Monitor colour mode", model=Gtk.StringList.new(names),
                           subtitle="RGB gains usually only work in User. sRGB is the monitor's own accurate mode.")
        row.set_selected(codes.index(d.ddc_preset))

        def changed(r, _):
            code = codes[r.get_selected()]
            if code != d.ddc_preset:
                d.set_preset(code)
                self.toast(f"Monitor switched to {names[r.get_selected()]}")
        row.connect("notify::selected", changed)
        return row

    def _profile_mode_group(self, d):
        grp = Adw.PreferencesGroup(title="Colour profile",
                                   description="Your desktop also uses this profile for colour management.")
        row = Adw.ComboRow(title="Profile describes", model=Gtk.StringList.new([m[1] for m in PROFILE_MODES]))
        keys = [m[0] for m in PROFILE_MODES]
        row.set_selected(keys.index(d.profile_mode) if d.profile_mode in keys else 0)
        row.set_subtitle(PROFILE_MODES[row.get_selected()][2])

        def changed(r, _):
            mode = keys[r.get_selected()]
            r.set_subtitle(PROFILE_MODES[r.get_selected()][2])
            d.set_profile_mode(mode)
            self.save_later()
        row.connect("notify::selected", changed)
        grp.add(row)
        return grp

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
        scroller, box = self.page_box()
        top = Gtk.Box(spacing=16)
        t = intro("Full-screen test patterns. Press one to check one thing, or run the guided calibration to "
                  "go through them all with the right controls beside each one.")
        t.set_hexpand(True)
        top.append(t)
        top.append(pill("Start guided calibration", True, lambda: self.open_patterns("black")))
        self.responsive(top, "orientation", narrow=Gtk.Orientation.VERTICAL)
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
            names = ", ".join(dict.fromkeys(c.label for c in self.controls if sid in c.steps))
            if sid != "uniformity":
                inner.append(label(f"Adjust here: {names}" if names else "Look only", "ct-overline", wrap=True))
            card.set_child(inner)
            card.connect("clicked", lambda _b, s=sid: self.open_patterns(s, single=True))
            flow.append(card)
        box.append(flow)
        return scroller

    # ---- measure
    def build_measure(self):
        if not measure.available():
            return Adw.StatusPage(icon_name="find-location-symbolic", title="Install ArgyllCMS to measure",
                                  description="A colorimeter is a small sensor that sits on the screen and measures "
                                              "its light. It's the only way to know colours are right, rather than "
                                              "looking right to tired eyes. ColorTune uses ArgyllCMS to talk to it; "
                                              "the installer sets it up.")
        bin_ = self.new_page()
        bin_.set_child(MeasurePage(self))
        return bin_

    # ---- profiles
    def build_profiles(self):
        scroller, box = self.page_box()
        box.append(intro("Save the current settings of every display under a name, for example Movies, Night or "
                         "Photo editing, and switch between them."))
        profiles = config.load_profiles()
        save = Adw.PreferencesGroup(
            description=None if profiles else "Nothing saved yet. Name your current settings, e.g. Night or Photo editing.")
        entry = Adw.EntryRow(title="Name for current settings")
        save_btn = Gtk.Button(label="Save", valign=Gtk.Align.CENTER, sensitive=False)
        save_btn.add_css_class("suggested-action")

        def do_save(*_):
            name = entry.get_text().strip()
            if name:
                self.save_profile(name)
        entry.connect("changed", lambda e: save_btn.set_sensitive(bool(e.get_text().strip())))
        entry.connect("entry-activated", do_save)
        save_btn.connect("clicked", do_save)
        entry.add_suffix(save_btn)
        save.add(entry)
        box.append(save)
        if profiles:
            grp = Adw.PreferencesGroup(title="Saved")
            for name in sorted(profiles):
                n = len(profiles[name])
                row = Adw.ActionRow(title=name, subtitle=f"{n} display{'s' if n != 1 else ''}")
                apply_btn = Gtk.Button(label="Apply", valign=Gtk.Align.CENTER)
                apply_btn.connect("clicked", lambda _b, nm=name: self.load_profile(nm))
                delete = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Delete")
                delete.add_css_class("flat")
                delete.connect("clicked", lambda _b, nm=name: self.delete_profile(nm))
                row.add_suffix(apply_btn)
                row.add_suffix(delete)
                grp.add(row)
            box.append(grp)
        presets = Adw.PreferencesGroup(title="Built in")
        row = Adw.ActionRow(title="Accurate",
                            subtitle="Neutral curves and saturation, and any calibration you had before ColorTune put back")
        b = Gtk.Button(label="Apply", valign=Gtk.Align.CENTER)
        b.connect("clicked", lambda *_: self.apply_accurate())
        row.add_suffix(b)
        presets.add(row)
        box.append(presets)
        startup = Adw.PreferencesGroup(title="Startup")
        sw = Adw.SwitchRow(title="Apply at login",
                           subtitle="Re-apply your current settings each time you sign in. Backlight is left as it is.",
                           active=config.autostart_enabled())
        sw.connect("notify::active", lambda s, _: config.set_autostart(s.get_active(), launcher_command()))
        startup.add(sw)
        box.append(startup)
        return scroller

    # ------------------------------------------------------------ actions
    def _changed(self, control):
        if control.key == "saturation" and getattr(self, "sat_stat", None):
            self.sat_stat.set_text(f"{control.value * 100:.0f}%")
        self.save_later()
        GLib.timeout_add(800, self._show_error)

    def _show_error(self):
        err = self.current.last_error if self.current else None
        if getattr(self, "error", None):
            self.error.set_text(err or "")
        if err and "colour filter" in err:
            self.toast(err)
        return False

    def refresh_banner(self, then=None):
        """Night Light check off the main thread (it runs subprocesses)."""
        if not self.env:
            return

        def work():
            nl = self.env.refresh_night_light()
            GLib.idle_add(self._show_banner, nl, then)
        threading.Thread(target=work, daemon=True).start()

    def _show_banner(self, nl, then=None):
        if nl:
            if nl["can_pause"]:
                self.banner.set_title(f"{nl['short']}. Colours look warmer than they are.")
                self.banner.set_button_label("Pause Night Light")
            else:
                self.banner.set_title(f"{nl['short']}. Turn it off in your desktop settings while you calibrate.")
                self.banner.set_button_label(None)
        self.banner.set_revealed(bool(nl))
        if then:
            then(nl)
        return False

    def pause_night_light(self):
        token = screenfilters.pause_night_light()
        if not token:
            self.toast("Couldn't pause Night Light")
            return False
        self.night_token = token
        self.toast("Night Light paused while ColorTune is open", "Turn back on", self.resume_night_light, timeout=6)
        # The compositor updates its state a moment later.
        for ms in (400, 1500):
            GLib.timeout_add(ms, lambda: (self.refresh_banner(), False)[1])
        return True

    def resume_night_light(self):
        screenfilters.resume_night_light(self.night_token)
        self.night_token = None
        GLib.timeout_add(600, lambda: (self.refresh_banner(), False)[1])

    def open_patterns(self, start="black", single=False):
        d = self.current
        win = PatternWindow(self.get_application(), self.controls, self._changed, start=start, single=single,
                            monitor=monitor_for(d.connector) if d else None, internal=bool(d and d.internal),
                            night=self.env.night_light if self.env else None, on_pause=self.pause_night_light)
        win.connect("close-request", lambda *_: (self._resync(), False)[1])
        win.present()
        return win

    def _resync(self):
        for s in self.sliders:
            s.sync()
        sat = next((c for c in self.controls if c.key == "saturation"), None)
        if sat and getattr(self, "sat_stat", None):
            self.sat_stat.set_text(f"{sat.value * 100:.0f}%")
        self.refresh_banner()

    def save_state(self):
        def update(state):
            for d in self.displays:
                state["displays"][d.key] = d.to_state()
        config.update_state(update)

    def _on_close(self, *_):
        WORKER.flush(timeout=3)
        self.save_state()
        if self.night_token:  # only if nothing else changed Night Light meanwhile
            screenfilters.resume_night_light(self.night_token, only_if_unchanged=True)
        return False

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
        targets = [d for d in self.displays if d.key in prof]
        for d in targets:
            d.load(prof[d.key], from_profile=True)
            d.apply_all(force=True)
        apply_desktop_saturation(targets)
        self.save_state()
        WORKER.flush(timeout=3)  # so the rebuilt sliders read the new hardware values
        self.toast(f"Applied “{name}”")
        self.rebuild()

    def apply_accurate(self):
        for d in self.displays:
            d.reset()
        WORKER.flush(timeout=5)
        self.save_state()
        self.toast("Back to neutral")
        self.rebuild()


class MeasurePage(Gtk.ScrolledWindow):
    """Colorimeter readings and automatic white balance via ArgyllCMS."""

    def __init__(self, main):
        super().__init__(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        self.main = main
        self.d = main.current
        self.white = self.black = None
        self.types = []
        self.cancel = None
        clamp, box = column()
        self.set_child(clamp)
        box.append(intro("Let the screen warm up for about 20 minutes. When a patch appears, lay the sensor "
                         "flat in its centre."))

        self.meter = Adw.PreferencesGroup(title="Colorimeter")
        self.meter_row = Adw.ActionRow(title="Looking for a colorimeter…")
        again = Gtk.Button(icon_name="view-refresh-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Look again")
        again.add_css_class("flat")
        again.connect("clicked", lambda *_: self._probe())
        self.meter_row.add_suffix(again)
        self.meter.add(self.meter_row)
        self.type_row = Adw.ComboRow(title="Screen type", visible=False,
                                     subtitle="Matching the backlight type makes readings more accurate")
        self.meter.add(self.type_row)
        box.append(self.meter)

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
        main.responsive(tiles, "orientation", Gtk.Orientation.VERTICAL)
        box.append(tiles)
        self.detail = label("", "dim-label", wrap=True)
        self.detail.set_visible(False)
        box.append(self.detail)
        self.primary = pill("Measure white and black", True, self.m_both)
        self.primary.set_halign(Gtk.Align.START)
        box.append(self.primary)

        grp = Adw.PreferencesGroup(title="Single readings")
        self.rows = {}
        for key, title, sub, fn in (("white", "Measure white", "Brightness and colour temperature", self.m_white),
                                    ("black", "Measure black", "How deep the blacks are, and the contrast ratio",
                                     self.m_black),
                                    ("wb", "Auto white balance", "Adjusts the RGB gains until white is D65 (6504 K)",
                                     self.auto_wb)):
            row = Adw.ActionRow(title=title, subtitle=sub, activatable=True)
            row.add_suffix(Gtk.Image(icon_name="media-playback-start-symbolic"))
            row.connect("activated", lambda _r, f=fn: f())
            self.rows[key] = row
            grp.add(row)
        if not self._gain_controls()[0]:
            self.rows["wb"].set_sensitive(False)
            self.rows["wb"].set_subtitle("Needs RGB gain controls, which this display doesn't have")
        box.append(grp)
        self._set_ready(False)
        self._probe()

    # ---- meter detection
    def _probe(self):
        self.meter_row.set_title("Looking for a colorimeter…")
        self.meter_row.set_subtitle("")

        def work():
            info = measure.probe()
            GLib.idle_add(self._probed, info)
        threading.Thread(target=work, daemon=True).start()

    def _probed(self, info):
        found = info["instruments"]
        if found:
            self.meter_row.set_title(found[0])
            self.meter_row.set_subtitle("Connected")
        else:
            self.meter_row.set_title("No colorimeter connected")
            self.meter_row.set_subtitle("Plug in a supported meter, such as a Calibrite Display Plus HL, "
                                        "Display Pro or SpyderX, then press refresh")
        self.types = info["types"]
        if self.types:
            self.type_row.set_model(Gtk.StringList.new([t[1] for t in self.types]))
            default = next((i for i, t in enumerate(self.types) if "default" in t[1].lower()), 0)
            self.type_row.set_selected(default)
        self.type_row.set_visible(bool(self.types))
        self._set_ready(bool(found))
        return False

    def _set_ready(self, ready):
        self.primary.set_sensitive(ready)
        for key, row in self.rows.items():
            if key != "wb" or self._gain_controls()[0]:
                row.set_sensitive(ready)

    def _display_type(self):
        if not self.types or not self.type_row.get_visible():
            return None
        return self.types[self.type_row.get_selected()][0]

    # ---- readings
    def _patch(self, rgb, msg):
        self.cancel = threading.Event()
        win = PatternWindow(self.main.get_application(), [], solid=rgb, message=msg,
                            monitor=monitor_for(self.d.connector))
        cancel = self.cancel
        win.connect("close-request", lambda *_: (cancel.set(), False)[1])
        win.present()
        return win, cancel

    def _read(self, cancel):
        return measure.read(self._display_type(), cancel)

    def _run(self, rgb, msg, then):
        win, cancel = self._patch(rgb, msg)
        dtype = self._display_type()

        def work():
            time.sleep(1.5)
            try:
                xyz = measure.read(dtype, cancel)
                if not cancel.is_set():
                    GLib.idle_add(then, xyz, win)
            except measure.Cancelled:
                pass
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
            quality = "excellent" if w["dxy"] < 0.003 else "good" if w["dxy"] < 0.006 else "needs balancing"
            self.detail.set_text(f"White x {w['x']:.4f}, y {w['y']:.4f}. Distance from D65: Δxy {w['dxy']:.4f} "
                                 f"({quality}).")
            self.detail.set_visible(True)
        if self.black:
            self.tiles["black"].set_text(f"{self.black[1]:.3f}")
        if self.white and self.black and self.black[1] > 0:
            self.tiles["contrast"].set_text(f"{self.white[1] / self.black[1]:,.0f}:1")

    def m_white(self, then=None):
        def done(xyz, win):
            win.close()
            self.white = xyz
            self._report()
            if then:
                then()
        self._run((1, 1, 1), "Measuring white…", done)

    def m_black(self):
        def done(xyz, win):
            win.close()
            self.black = xyz
            self._report()
        self._run((0, 0, 0), "Measuring black…", done)

    def m_both(self):
        self.m_white(then=self.m_black)

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
            return
        win, cancel = self._patch((1, 1, 1), "Auto white balance. Keep the sensor in place…")
        prim = self.d.edid.get("primaries") if self.d.edid_problem() is None else None
        dtype = self._display_type()
        before = [c.value for c in controls]

        def work():
            try:
                xyz, first = None, None
                passes = 7
                for i in range(passes):
                    time.sleep(1.5)
                    if cancel.is_set():
                        raise measure.Cancelled()
                    xyz = measure.read(dtype, cancel)
                    info = measure.describe(xyz)
                    if info["cct"] is None:
                        raise RuntimeError("no light detected. Place the sensor flat on the white patch")
                    first = info["dxy"] if first is None else first
                    GLib.idle_add(win.set_message, f"Pass {i + 1}: {info['cct']:.0f} K, Δxy {info['dxy']:.4f}")
                    if info["dxy"] < 0.002 or i == passes - 1:
                        break  # the last pass only measures the result of the last change
                    gains = [c.value / c.maximum for c in controls]
                    new = measure.balance(xyz, gains, linear=linear, primaries=prim)
                    if cancel.is_set():
                        raise measure.Cancelled()
                    for c, g in zip(controls, new):
                        c.value = min(c.maximum, max(c.minimum, round(g * c.maximum / c.step) * c.step))
                        c.setter(c.value)
                    WORKER.flush()
                final = measure.describe(xyz)["dxy"]
                if final >= 0.005 and final > first * 0.7:
                    raise RuntimeError("white didn't move towards D65. Put the monitor in its User/Custom "
                                       "colour mode and turn Night Light off, then try again")
                self.white = xyz
                GLib.idle_add(self._wb_done, win)
            except measure.Cancelled:
                GLib.idle_add(self._wb_undo, controls, before, "White balance cancelled; gains put back")
            except Exception as e:
                GLib.idle_add(self._wb_undo, controls, before, None)
                GLib.idle_add(self._fail, str(e), win)
        threading.Thread(target=work, daemon=True).start()

    def _wb_undo(self, controls, before, msg):
        for c, v in zip(controls, before):
            c.value = v
            c.setter(v)
        self.main._resync()
        if msg:
            self.main.toast(msg)
        return False

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
    if (Adw.get_major_version(), Adw.get_minor_version()) < (1, 4):
        msg = (f"ColorTune needs libadwaita 1.4 or newer (found {Adw.get_major_version()}."
               f"{Adw.get_minor_version()}). The command line still works: colortune detect / set / apply.")
        print(msg, file=sys.stderr)
        return 1
    return App().run([sys.argv[0]])
