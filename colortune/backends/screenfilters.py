"""Desktop screen filters: Night Light detection everywhere, Ada's colour filters.

Night Light / Night Color / redshift-style warming silently ruins calibration, so we
detect it on Ada, GNOME and KDE and can pause it. On Ada the compositor also draws the
screen through an optional greyscale filter whose intensity we use as a saturation
control (it can only reduce saturation - Ada has no way to boost it).
"""
import os
import re
import time

from . import run, which

ADA_INI = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "os-ai", "desktop.ini")


def desktop():
    return os.environ.get("XDG_CURRENT_DESKTOP", "").upper()


# ---------------------------------------------------------------- Ada desktop.ini

def ada_available():
    return "ADA" in desktop().split(":") and os.path.exists(ADA_INI)


def _ada_read():
    try:
        with open(ADA_INI, newline="") as f:
            return f.read()
    except OSError:
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

    Written in place (same inode) because the shell and compositor watch the file.
    """
    text = _ada_read()
    sec = _desktop_section(text)
    if not sec:
        return False
    nl = "\r\n" if "\r\n" in text else "\n"
    body = text[sec[0]:sec[1]]
    for key, value in values.items():
        line = f"{key}={value}"
        pat = rf"^{re.escape(key)}=.*?(?=\r?$)"
        if re.search(pat, body, re.M):
            body = re.sub(pat, lambda _m: line, body, count=1, flags=re.M)
        else:
            body = line + nl + body
    text = text[:sec[0]] + body + text[sec[1]:]
    with open(ADA_INI, "r+", newline="") as f:
        f.seek(0)
        f.write(text)
        f.truncate()
    return True


def ada_filter_state():
    """(enabled, filter_name, strength%)"""
    return (ada_get("ColorFilters", "off") == "on", ada_get("ColorFilter", "grayscale"),
            int(ada_get("ColorFilterStrength", "100") or 100))


def ada_saturation_usable():
    """False when the user runs another filter (e.g. for colour blindness) we must not replace."""
    on, name, _ = ada_filter_state()
    return not on or name == "grayscale"


def ada_get_saturation():
    on, name, strength = ada_filter_state()
    return 1.0 - strength / 100 if on and name == "grayscale" else 1.0


def ada_set_saturation(s):
    s = max(0.0, min(1.0, float(s)))
    if s > 0.95:
        ada_set({"ColorFilters": "off"})
    else:
        # Ada's intensity runs 10-100%
        ada_set({"ColorFilters": "on", "ColorFilter": "grayscale",
                 "ColorFilterStrength": max(10, round((1 - s) * 100))})


# ---------------------------------------------------------------- Night Light everywhere

def night_light():
    """{"desktop", "on", "detail", "can_pause"} or None if no night light is known to be on."""
    d = desktop()
    if ada_available():
        if ada_get("NightLight", "off") == "on":
            k = ada_get("NightLightTemperature")
            return {"desktop": "Ada", "on": True, "can_pause": True,
                    "detail": f"Night Light is warming the screen{f' to {k} K' if k else ''}"}
    elif "GNOME" in d and which("gsettings"):
        rc, out, _ = run(["gsettings", "get", "org.gnome.settings-daemon.plugins.color", "night-light-enabled"])
        if out.strip() == "true":
            rc, k, _ = run(["gsettings", "get", "org.gnome.settings-daemon.plugins.color", "night-light-temperature"])
            return {"desktop": "GNOME", "on": True, "can_pause": True,
                    "detail": "Night Light is enabled" + (f" ({k.split()[-1]} K)" if k.strip() else "")}
    elif "KDE" in d:
        tool = which("kreadconfig6") or which("kreadconfig5")
        if tool:
            rc, out, _ = run([tool, "--file", "kwinrc", "--group", "NightColor", "--key", "Active"])
            if out.strip() == "true":
                return {"desktop": "KDE", "on": True, "can_pause": False,
                        "detail": "Night Light is enabled in System Settings"}
    for proc in ("gammastep", "redshift", "wlsunset", "hyprsunset", "sunsetr"):
        rc, out, _ = run(["pgrep", "-x", proc])
        if rc == 0:
            return {"desktop": proc, "on": True, "can_pause": False, "detail": f"{proc} is running and tints the screen"}
    return None


def pause_night_light():
    """Turn night light off now. Returns True if done."""
    if ada_available():
        # Same keys the Settings app writes: off now, until the schedule's next change.
        return ada_set({"NightLight": "off", "NightLightChanged": int(time.time())})
    if "GNOME" in desktop() and which("gdbus"):
        # What the quick-settings toggle does: off until tomorrow, schedule kept.
        return run(["gdbus", "call", "--session", "--dest", "org.gnome.SettingsDaemon.Color",
                    "--object-path", "/org/gnome/SettingsDaemon/Color",
                    "--method", "org.freedesktop.DBus.Properties.Set",
                    "org.gnome.SettingsDaemon.Color", "DisabledUntilTomorrow", "<true>"])[0] == 0
    return False
