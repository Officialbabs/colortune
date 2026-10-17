"""Desktop screen filters: Night Light detection everywhere, Ada's colour filters.

Night Light / Night Color / redshift-style warming silently ruins calibration, so we
detect when it is *active* on Ada, GNOME and KDE and can pause it. On Ada the compositor
also draws the screen through an optional greyscale filter whose intensity we use as a
saturation control (it can only reduce saturation - Ada has no way to boost it).

The colour filters are accessibility settings, so ColorTune only ever drives the filter
while it owns it: it remembers the user's own filter choice when it takes over and puts
it back when saturation returns to neutral, and never touches a filter the user turned on.
"""
import json
import os
import re
import tempfile
import threading
import time

from . import run, which
from .. import config

ADA_INI = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "os-ai", "desktop.ini")
_LOCK = threading.RLock()
_ENC = {"encoding": "utf-8", "errors": "surrogateescape", "newline": ""}


def desktop():
    return os.environ.get("XDG_CURRENT_DESKTOP", "").upper()


# ---------------------------------------------------------------- Ada desktop.ini

def ada_available():
    return "ADA" in desktop().split(":") and os.path.exists(ADA_INI)


def _ada_read():
    try:
        with open(ADA_INI, **_ENC) as f:
            return f.read()
    except (OSError, UnicodeError):
        return ""


def _desktop_section(text):
    """(start, end) of the [Desktop] section body, or None."""
    m = re.search(r"^\[Desktop\][ \t]*(?:\r?\n|\Z)", text, re.M)
    if not m:
        return None
    nxt = re.search(r"^\[", text[m.end():], re.M)
    return m.end(), m.end() + nxt.start() if nxt else len(text)


def ada_get(key, default=None):
    text = _ada_read()
    sec = _desktop_section(text)
    if not sec:
        return default
    m = re.search(rf"^{re.escape(key)}=(.*?)\r?$", text[sec[0]:sec[1]], re.M)
    return m.group(1).strip() if m else default


def ada_set(values):
    """Update keys in [Desktop], keeping everything else byte-for-byte.

    Saved the way Ada saves it (temp file + rename, mode 0600), so the shell and
    compositor never see a half-written file.
    """
    with _LOCK:
        old = _ada_read()
        sec = _desktop_section(old)
        if not sec:
            return False
        nl = "\r\n" if "\r\n" in old else "\n"
        head, body, tail = old[:sec[0]], old[sec[0]:sec[1]], old[sec[1]:]
        if not head.endswith("\n"):  # header was the file's last line
            head += nl
        for key, value in values.items():
            line = f"{key}={value}"
            pat = rf"^{re.escape(key)}=.*?(?=\r?$)"
            if re.search(pat, body, re.M):
                body = re.sub(pat, lambda _m: line, body, count=1, flags=re.M)
            else:
                body = line + nl + body
        new = head + body + tail
        if new == old:
            return True
        d = os.path.dirname(ADA_INI)
        try:
            mode = os.stat(ADA_INI).st_mode & 0o777
        except OSError:
            mode = 0o600
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".desktop.ini.")
        try:
            os.fchmod(fd, mode)
            with os.fdopen(fd, "w", **_ENC) as f:
                f.write(new)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, ADA_INI)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return True


def ada_filter_state():
    """(enabled, filter_name, strength%)"""
    try:
        strength = int(float(ada_get("ColorFilterStrength", "100")))
    except (TypeError, ValueError):
        strength = 100
    return (ada_get("ColorFilters", "off") == "on", ada_get("ColorFilter", "grayscale"),
            max(10, min(100, strength)))


def _owner_record():
    return config.load_state().get("ada_filter")


def _set_owner_record(rec):
    def update(state):
        if rec is None:
            state.pop("ada_filter", None)
        else:
            state["ada_filter"] = rec
    config.update_state(update)


def ada_saturation_status():
    """'free' (filter off), 'ours' (the greyscale ColorTune set) or 'theirs' (anything else on).

    'ours' needs the exact strength ColorTune wrote, so a greyscale the user turned on
    themselves - even after a stale record - is never mistaken for ColorTune's.
    """
    with _LOCK:
        on, name, strength = ada_filter_state()
        rec = _owner_record()
        if not on:
            if rec:  # the user turned our filter off in Ada Settings: put their choice back
                ada_set(rec.get("saved", {}))
                _set_owner_record(None)
            return "free"
        if rec and name == "grayscale" and strength == rec.get("wrote"):
            return "ours"
        return "theirs"


def ada_saturation_usable():
    return ada_saturation_status() != "theirs"


def ada_get_saturation():
    on, name, strength = ada_filter_state()
    return round(1.0 - strength / 100, 2) if ada_saturation_status() == "ours" else 1.0


def snap_saturation(s):
    """Values Ada can show: off (1.0) or greyscale intensity 10-100% in steps of 10."""
    s = float(s)
    return 1.0 if s > 0.95 else round(max(0.0, min(0.9, s)), 1)


def ada_set_saturation(s):
    """Apply saturation through Ada's filter. Returns the value applied, or None if refused."""
    s = snap_saturation(s)
    with _LOCK:
        status = ada_saturation_status()
        if status == "theirs":
            return None
        if s >= 1.0:
            if status == "ours":
                saved = (_owner_record() or {}).get("saved", {})
                ada_set({"ColorFilters": "off", **saved})
                _set_owner_record(None)
            return 1.0
        rec = _owner_record()
        if status == "free" or not rec:
            _, name, strength = ada_filter_state()
            rec = {"saved": {"ColorFilter": name, "ColorFilterStrength": strength}}
        strength = round((1 - s) * 100)
        ada_set({"ColorFilters": "on", "ColorFilter": "grayscale", "ColorFilterStrength": strength})
        _set_owner_record({**rec, "wrote": strength})
        return s


# ---------------------------------------------------------------- Night Light everywhere

def _gdbus_props(dest, path, iface):
    if not which("gdbus"):
        return None
    rc, out, _ = run(["gdbus", "call", "--session", "--dest", dest, "--object-path", path,
                      "--method", "org.freedesktop.DBus.Properties.GetAll", iface], timeout=3)
    if rc != 0:
        return None
    props = {}
    for m in re.finditer(r"'(\w+)': <(?:uint32 |int32 |double )?([^>]*)>", out):
        props[m.group(1)] = m.group(2).strip("'")
    return props


def _ada_live():
    """Ada compositor's own word on Night Light: {"on", "kelvin", "until", ...} or None."""
    path = os.path.join(os.environ.get("XDG_RUNTIME_DIR", ""), "ada-night-light",
                        os.environ.get("WAYLAND_DISPLAY", "wayland-0") + ".json")
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _result(desktop_name, kelvin, can_pause, until=None):
    k = f" ({int(float(kelvin))} K)" if kelvin else ""
    short = f"Night Light is on{k}"
    if until:
        short += " until " + time.strftime("%H:%M", time.localtime(until))
    return {"desktop": desktop_name, "on": True, "can_pause": can_pause, "short": short,
            "detail": f"Night Light is warming the screen{k}"}


def night_light(gamma_tools=False):
    """Active screen warming as a dict, or None. gamma_tools: also look for
    redshift-style programs (only meaningful on X11 or wlroots compositors)."""
    d = desktop()
    if ada_available():
        live = _ada_live()
        if live is not None:
            if live.get("on"):
                return _result("Ada", live.get("kelvin"), True, live.get("until"))
            return None
        if ada_get("NightLight", "off") == "on":
            return _result("Ada", ada_get("NightLightTemperature"), True)
        return None
    if "GNOME" in d or "UNITY" in d or "BUDGIE" in d:
        p = _gdbus_props("org.gnome.SettingsDaemon.Color", "/org/gnome/SettingsDaemon/Color",
                         "org.gnome.SettingsDaemon.Color")
        if p is not None and "NightLightActive" in p:
            if p.get("NightLightActive") == "true" and p.get("DisabledUntilTomorrow") != "true":
                return _result("GNOME", p.get("Temperature"), True)
            return None
    if "KDE" in d:
        for path, iface in (("/org/kde/KWin/NightLight", "org.kde.KWin.NightLight"),
                            ("/ColorCorrect", "org.kde.kwin.ColorCorrect")):
            if not which("gdbus"):
                break
            rc, out, _ = run(["gdbus", "call", "--session", "--dest", "org.kde.KWin", "--object-path", path,
                              "--method", "org.freedesktop.DBus.Properties.Get", iface, "running"], timeout=3)
            if rc == 0:
                if "true" in out:
                    rc2, t, _ = run(["gdbus", "call", "--session", "--dest", "org.kde.KWin", "--object-path",
                                     path, "--method", "org.freedesktop.DBus.Properties.Get", iface,
                                     "currentTemperature"], timeout=3)
                    m = re.search(r"<(?:u?int32 |double )?(\d+)", t)
                    return _result("KDE", m.group(1) if m else None, False)
                return None
    if gamma_tools:
        for proc in ("gammastep", "redshift", "wlsunset", "hyprsunset", "sunsetr"):
            rc, _, _ = run(["pgrep", "-x", "-u", str(os.getuid()), proc])
            if rc == 0:
                return {"desktop": proc, "on": True, "can_pause": False,
                        "short": f"{proc} is tinting the screen",
                        "detail": f"{proc} is running and tints the screen"}
    return None


def pause_night_light():
    """Turn Night Light off now, if it is on. Returns a token for resume_night_light, or None."""
    if not night_light():
        return None
    if ada_available():
        prev = {"NightLight": ada_get("NightLight"), "NightLightChanged": ada_get("NightLightChanged")}
        stamp = int(time.time())
        # The same keys Ada's own toggle writes: off now, until the schedule's next change.
        if ada_set({"NightLight": "off", "NightLightChanged": stamp}):
            return {"desktop": "Ada", "stamp": stamp, "prev": prev}
        return None
    if which("gdbus") and run(["gdbus", "call", "--session", "--dest", "org.gnome.SettingsDaemon.Color",
                               "--object-path", "/org/gnome/SettingsDaemon/Color",
                               "--method", "org.freedesktop.DBus.Properties.Set",
                               "org.gnome.SettingsDaemon.Color", "DisabledUntilTomorrow", "<true>"])[0] == 0:
        # What GNOME's quick-settings toggle does: off until tomorrow, schedule kept.
        return {"desktop": "GNOME"}
    return None


def resume_night_light(token, only_if_unchanged=False):
    """Undo pause_night_light(token).

    On Ada the previous keys are written back, so the compositor works the state out
    from the schedule as if the pause never happened (no Night Light in daylight).
    With only_if_unchanged, leave everything alone if anything changed it since.
    """
    if not token:
        return False
    if token["desktop"] == "Ada":
        with _LOCK:
            if only_if_unchanged and (ada_get("NightLight") != "off"
                                      or ada_get("NightLightChanged") != str(token["stamp"])):
                return False
            prev = {k: v for k, v in token.get("prev", {}).items() if v is not None}
            return ada_set(prev or {"NightLight": "on", "NightLightChanged": int(time.time())})
    if token["desktop"] == "GNOME":
        if only_if_unchanged:
            p = _gdbus_props("org.gnome.SettingsDaemon.Color", "/org/gnome/SettingsDaemon/Color",
                             "org.gnome.SettingsDaemon.Color")
            if not p or p.get("DisabledUntilTomorrow") != "true":
                return False
        return run(["gdbus", "call", "--session", "--dest", "org.gnome.SettingsDaemon.Color",
                    "--object-path", "/org/gnome/SettingsDaemon/Color",
                    "--method", "org.freedesktop.DBus.Properties.Set",
                    "org.gnome.SettingsDaemon.Color", "DisabledUntilTomorrow", "<false>"])[0] == 0
    return False
