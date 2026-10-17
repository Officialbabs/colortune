"""Neutral-grey look.

A calibration tool must not tint what you're judging: coloured chrome and a bluish
dark theme shift how white and grey look. So every surface here is a true neutral
grey, the accent is white, and colour only appears in the content itself.
"""
import os

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gtk  # noqa: E402

from .. import APP_ID  # noqa: E402

ROOT_CSS = """
:root {
  --window-bg-color: #1c1c1c; --window-fg-color: #ececec;
  --view-bg-color: #1c1c1c; --view-fg-color: #ececec;
  --headerbar-bg-color: #1c1c1c; --headerbar-fg-color: #ececec; --headerbar-shade-color: rgba(0,0,0,.6);
  --sidebar-bg-color: #141414; --sidebar-fg-color: #ececec; --sidebar-shade-color: rgba(0,0,0,.6);
  --card-bg-color: rgba(255,255,255,.045); --card-fg-color: #ececec;
  --popover-bg-color: #2a2a2a; --dialog-bg-color: #232323;
  --accent-bg-color: #ececec; --accent-fg-color: #141414; --accent-color: #f4f4f4;
  --accent-color-default: #f4f4f4;
}
"""

CSS = """
@define-color ct_dim #a0a0a0;
@define-color window_bg_color #1c1c1c;
@define-color view_bg_color #1c1c1c;
@define-color headerbar_bg_color #1c1c1c;
@define-color sidebar_bg_color #141414;
@define-color card_bg_color rgba(255,255,255,.045);
@define-color popover_bg_color #2a2a2a;
@define-color accent_bg_color #ececec;
@define-color accent_fg_color #141414;
@define-color accent_color #f4f4f4;

.ct-overline { font-size: 0.78em; font-weight: 700; letter-spacing: 0.12em; color: @ct_dim; }
.ct-display-title { font-size: 1.9em; font-weight: 800; letter-spacing: -0.01em; }
.ct-stat { font-size: 1.9em; font-weight: 800; font-feature-settings: "tnum"; letter-spacing: -0.02em; }
.ct-stat-label { font-size: 0.85em; color: @ct_dim; }
.ct-hero { background: rgba(255,255,255,.035); border-radius: 18px; padding: 22px; }
.ct-note { background: rgba(255,255,255,.06); border-radius: 12px; padding: 12px 14px; }
.ct-footnote { font-size: 0.85em; color: @ct_dim; }
.ct-value { font-feature-settings: "tnum"; font-weight: 600; opacity: 0.8; min-width: 3.5em; }
.ct-tip { font-size: 0.9em; color: @ct_dim; }
.ct-slider-row { padding: 10px 14px 8px 14px; }
.ct-ok { color: #f4f4f4; }
.ct-no { color: @ct_dim; }

/* Slider tracks show what they change */
scale.ct-track trough { min-height: 8px; border-radius: 4px; }
scale.ct-track highlight { background: transparent; box-shadow: none; }
scale.ct-track slider { background: #f2f2f2; border: 1px solid rgba(0,0,0,.6);
  box-shadow: 0 1px 3px rgba(0,0,0,.6); min-width: 18px; min-height: 18px; }
scale.ct-track-red trough   { background-image: linear-gradient(to right, #2a1414, #ff4b4b); }
scale.ct-track-green trough { background-image: linear-gradient(to right, #142a16, #3ddc5a); }
scale.ct-track-blue trough  { background-image: linear-gradient(to right, #14182a, #4b6bff); }
scale.ct-track-black trough { background-image: linear-gradient(to right, #000000, #4a4a4a); }
scale.ct-track-tone trough  { background-image: linear-gradient(to right, #000000, #808080, #ffffff); }
scale.ct-track-light trough { background-image: linear-gradient(to right, #262626, #ffffff); }
/* grey washing out to vivid - no hue sweep, so it can't be mistaken for a hue control */
scale.ct-track-sat trough   { background-image: linear-gradient(to right, #8a8a8a, rgba(138,138,138,0)),
  linear-gradient(to right, #e5484d 0%, #e5484d 25%, #f2c94c 25%, #f2c94c 50%, #30a46c 50%, #30a46c 75%, #3e63dd 75%, #3e63dd 100%); }
scale.ct-track-neutral trough { background-image: linear-gradient(to right, #3a3a3a, #9a9a9a); }

.ct-card { background: rgba(255,255,255,.045); border-radius: 14px; padding: 10px; }
.ct-card:hover { background: rgba(255,255,255,.08); }
.ct-card-title { font-weight: 700; }
.ct-card label.ct-tip { font-weight: 400; }
.ct-thumb { border-radius: 8px; }

/* Pattern window panel */
.ct-panel { background: rgba(14,14,14,.94); color: #ececec; border-radius: 18px; padding: 18px 22px;
  border: 1px solid rgba(255,255,255,.08); box-shadow: 0 10px 40px rgba(0,0,0,.6); }
.ct-panel-title { font-size: 1.35em; font-weight: 800; }
.ct-dot { min-width: 8px; min-height: 8px; border-radius: 4px; background: rgba(255,255,255,.18); }
.ct-dot.active { background: #f4f4f4; min-width: 22px; }
.ct-key { font-family: monospace; font-size: 0.8em; padding: 1px 6px; border-radius: 5px;
  border: 1px solid rgba(255,255,255,.25); }
.ct-warn { background: rgba(255,255,255,.07); border: 1px solid rgba(255,255,255,.14);
  border-radius: 10px; padding: 8px 12px; }
.ct-dot.look { background: transparent; border: 1px solid rgba(255,255,255,.3); }
.ct-pattern-dock { background: #0e0e0e; }
.ct-osd { background: rgba(14,14,14,.9); color: #f4f4f4; border-radius: 999px; padding: 8px 18px;
  font-weight: 700; font-feature-settings: "tnum"; }
.ct-measure-value { font-size: 2.4em; font-weight: 800; font-feature-settings: "tnum"; }
"""


ICONS = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", "icons")


def install():
    Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
    if os.path.isdir(ICONS):  # running from a checkout: find our icon without installing
        Gtk.IconTheme.get_for_display(Gdk.Display.get_default()).add_search_path(ICONS)
    Gtk.Window.set_default_icon_name(APP_ID)
    css = CSS
    if (Gtk.get_major_version(), Gtk.get_minor_version()) >= (4, 16):
        css = ROOT_CSS + css  # CSS variables are only understood from GTK 4.16 / libadwaita 1.6
    prov = Gtk.CssProvider()
    if hasattr(prov, "load_from_string"):
        prov.load_from_string(css)
    else:
        prov.load_from_data(css.encode(), -1)
    Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), prov,
                                              Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)


TRACKS = {
    "red": "red", "vcp16": "red", "vcp6C": "red",
    "green": "green", "vcp18": "green", "vcp6E": "green",
    "blue": "blue", "vcp1A": "blue", "vcp70": "blue",
    "black": "black",
    "gamma": "tone", "contrast": "tone", "vcp12": "tone",
    "brightness": "light", "backlight": "light", "vcp10": "light",
    "saturation": "sat", "vcp8A": "sat",
}


def track_class(key):
    return "ct-track-" + TRACKS.get(key, "neutral")
