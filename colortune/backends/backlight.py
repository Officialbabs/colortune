"""Laptop / built-in panel backlight via logind (no root) or sysfs."""
import os

from . import run, which

SYSFS = "/sys/class/backlight"
INTERNAL_PREFIXES = ("eDP", "LVDS", "DSI")
# Prefer firmware/platform interfaces over raw, as the kernel docs recommend.
PRIORITY = {"firmware": 0, "platform": 1, "raw": 2}


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def find():
    """Best backlight device name, or None."""
    try:
        names = os.listdir(SYSFS)
    except OSError:
        return None
    names = [n for n in names if _read(os.path.join(SYSFS, n, "max_brightness"))]
    names.sort(key=lambda n: PRIORITY.get(_read(os.path.join(SYSFS, n, "type")), 3))
    return names[0] if names else None


class Backlight:
    def __init__(self, name):
        self.name = name
        self.path = os.path.join(SYSFS, name)
        self.max = int(_read(os.path.join(self.path, "max_brightness")) or 1)

    def get_percent(self):
        cur = int(_read(os.path.join(self.path, "brightness")) or 0)
        return round(100 * cur / self.max)

    def set_percent(self, pct):
        raw = max(1, round(self.max * float(pct) / 100))  # never fully off
        if self._logind(raw):
            return
        try:
            with open(os.path.join(self.path, "brightness"), "w") as f:
                f.write(str(raw))
            return
        except OSError:
            pass
        if which("brightnessctl"):
            run(["brightnessctl", "-d", self.name, "set", str(raw)])

    def _logind(self, raw):
        try:
            from gi.repository import Gio, GLib
            bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            bus.call_sync("org.freedesktop.login1", "/org/freedesktop/login1/session/auto",
                          "org.freedesktop.login1.Session", "SetBrightness",
                          GLib.Variant("(ssu)", ("backlight", self.name, raw)),
                          None, Gio.DBusCallFlags.NONE, 2000, None)
            return True
        except Exception:
            return False
