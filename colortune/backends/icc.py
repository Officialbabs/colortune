"""Apply settings as an ICC profile with a vcgt calibration curve.

GNOME (Wayland and X11) loads the vcgt of the colord default profile into the display;
KDE Plasma 6 loads per-output ICC profiles set with kscreen-doctor. On both, the
profile also drives colour management, so it must describe the screen honestly:

* "curves" mode (default) claims sRGB primaries - colours look exactly as they did,
  only the curves change. This is what kgamma2 does.
* "edid" mode uses the display's own reported primaries, if they pass a sanity check.

Whatever profile was the default before ColorTune's first change is remembered and
put back by restore() (Reset).
"""
import glob
import hashlib
import json
import os
import re

from . import run, which
from .. import config
from ..edid import validate_primaries
from ..icc import build_profile
from ..lut import ramps

_DATA = os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
ICC_DIR = os.path.join(_DATA, "icc")                   # colord's per-user store (import copies here)
OWN_DIR = os.path.join(_DATA, "colortune", "profiles")  # our copies; KDE references these directly
PREFIX = "colortune-"


def desktop():
    return os.environ.get("XDG_CURRENT_DESKTOP", "").upper()


def kde_available():
    return "KDE" in desktop() and bool(which("kscreen-doctor"))


def colord_available():
    return bool(which("colormgr"))


def gnome_applies_vcgt():
    return any(d in desktop() for d in ("GNOME", "UNITY", "BUDGIE", "PANTHEON", "CINNAMON"))


def _colord_device(connector, edid_md5=None):
    """Find the colord device object path for a connector."""
    rc, out, _ = run(["colormgr", "get-devices-by-kind", "display"])
    if rc != 0:
        return None
    for block in re.split(r"\n(?=Object Path:)", out):
        path = re.search(r"Object Path:\s*(\S+)", block)
        if not path:
            continue
        if (connector and re.search(rf"\b{re.escape(connector)}\b", block)) or \
                (edid_md5 and edid_md5 in block):
            return path.group(1)
    return None


def _profile_base(key):
    return PREFIX + re.sub(r"[^A-Za-z0-9_.-]", "_", key)


def profile_primaries(display, mode):
    """(primaries or None for sRGB, note) for the chosen mode."""
    prim = display.edid.get("primaries")
    if mode == "edid":
        problem = validate_primaries(prim)
        if problem is None:
            return prim, "the display's reported colours"
        return None, f"sRGB, because the display's colour data looks wrong ({problem})"
    return None, "sRGB (colours unchanged)"


def write_profile(display, picture, mode="curves"):
    """Write the ICC file for this display; returns its path."""
    os.makedirs(OWN_DIR, exist_ok=True)
    prim, _ = profile_primaries(display, mode)
    data = build_profile(f"ColorTune {display.name}", prim, 2.2,
                         None if picture.lut_identity() else ramps(picture, 256),
                         gains=(picture.red, picture.green, picture.blue))
    digest = hashlib.md5(data[128:]).hexdigest()[:8]
    base = _profile_base(display.key)
    path = os.path.join(OWN_DIR, f"{base}-{digest}.icc")
    _forget_all(display.key, keep=os.path.basename(path))
    if not os.path.exists(path):
        with open(path, "wb") as f:
            f.write(data)
    return path


def _find_colord_profile(filename):
    rc, out, _ = run(["colormgr", "find-profile-by-filename", filename])
    m = re.search(r"Object Path:\s*(\S+)", out)
    return m.group(1) if m else None


def _forget(path):
    if colord_available():
        obj = _find_colord_profile(os.path.basename(path))
        if obj:
            run(["colormgr", "delete-profile", obj])
    try:
        os.remove(path)
    except OSError:
        pass


def _forget_all(key, keep=None):
    base = _profile_base(key)
    for d in (OWN_DIR, ICC_DIR):
        for old in glob.glob(os.path.join(d, base + "-*.icc")):
            if os.path.basename(old) != keep:
                _forget(old)


# ---------------------------------------------------------------- previous profile

def _kde_output(connector):
    rc, out, _ = run(["kscreen-doctor", "-j"])
    try:
        for o in json.loads(out).get("outputs", []):
            if o.get("name") == connector:
                return o
    except ValueError:
        pass
    return {}


def _remember_previous(display, dev=None):
    """Record what was in use before ColorTune's first profile, once."""
    if display.key in config.load_state().get("icc_previous", {}):
        return
    if kde_available():
        o = _kde_output(display.connector)
        path = o.get("iccProfilePath") or ""
        rec = {"kde_path": "" if os.path.basename(path).startswith(PREFIX) else path,
               "kde_source": o.get("colorProfileSource")}
    elif dev:
        rc, out, _ = run(["colormgr", "device-get-default-profile", dev])
        obj = re.search(r"Object Path:\s*(\S+)", out)
        fname = re.search(r"Filename:\s*(\S+)", out)
        ours = fname and os.path.basename(fname.group(1)).startswith(PREFIX)
        rec = {"colord_profile": obj.group(1) if obj and not ours else None}
    else:
        return
    config.update_state(lambda st: st.setdefault("icc_previous", {}).setdefault(display.key, rec))


def restore(display):
    """Put back the profile that was in use before ColorTune, and remove ours.
    A display ColorTune never profiled is left exactly as it is."""
    prev = config.load_state().get("icc_previous", {}).get(display.key)
    if kde_available():
        if prev and prev.get("kde_path"):
            run(["kscreen-doctor", f"output.{display.connector}.iccprofile.{prev['kde_path']}"])
        elif prev or os.path.basename(_kde_output(display.connector).get("iccProfilePath") or "").startswith(PREFIX):
            source = (prev or {}).get("kde_source") or "sRGB"
            run(["kscreen-doctor", f"output.{display.connector}.colorProfileSource.{source}"])
    elif colord_available() and prev and prev.get("colord_profile"):
        dev = _colord_device(display.connector, display.edid.get("md5"))
        if dev:
            run(["colormgr", "device-make-profile-default", dev, prev["colord_profile"]])
    _forget_all(display.key)
    config.update_state(lambda st: st.get("icc_previous", {}).pop(display.key, None))
    return None


def apply(display, picture, mode="curves"):
    """Install the profile for a Display. Returns an error string or None."""
    if kde_available():
        _remember_previous(display)
        path = write_profile(display, picture, mode)
        rc, _, err = run(["kscreen-doctor", f"output.{display.connector}.iccprofile.{path}"])
        return (err.strip() or "kscreen-doctor failed") if rc else None
    if not colord_available():
        return "colord (colormgr) is not installed"
    dev = _colord_device(display.connector, display.edid.get("md5"))
    if not dev:
        return "this desktop has not registered the display with colord"
    _remember_previous(display, dev)
    path = write_profile(display, picture, mode)
    obj = _find_colord_profile(os.path.basename(path))
    if not obj:
        rc, out, err = run(["colormgr", "import-profile", path])
        obj = _find_colord_profile(os.path.basename(path))
        if not obj:
            return f"colord did not accept the profile: {err.strip()}"
    run(["colormgr", "device-add-profile", dev, obj])
    rc, _, err = run(["colormgr", "device-make-profile-default", dev, obj])
    return (err.strip() or "colord refused") if rc else None
