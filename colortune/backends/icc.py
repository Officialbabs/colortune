"""Apply settings as an ICC profile with a vcgt calibration curve.

GNOME (Wayland and X11) loads the vcgt of the colord default profile into the display;
KDE Plasma 6 loads per-output ICC profiles set with kscreen-doctor. Colour-managed apps
(GIMP, darktable, Krita, Firefox with gfx.color_management) also use the primaries.
"""
import glob
import hashlib
import os
import re

from . import run, which
from ..icc import build_profile
from ..lut import ramps

_DATA = os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
ICC_DIR = os.path.join(_DATA, "icc")                   # colord's per-user store (import copies here)
OWN_DIR = os.path.join(_DATA, "colortune", "profiles")  # our copies; KDE references these directly


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
    return f"colortune-{re.sub(r'[^A-Za-z0-9_.-]', '_', key)}"


def write_profile(key, name, info, picture):
    """Write the ICC file for this display; returns its path."""
    os.makedirs(OWN_DIR, exist_ok=True)
    data = build_profile(f"ColorTune {name}", info.get("primaries"), info.get("gamma", 2.2),
                         None if picture.lut_identity() else ramps(picture, 256))
    digest = hashlib.md5(data[128:]).hexdigest()[:8]
    base = _profile_base(key)
    path = os.path.join(OWN_DIR, f"{base}-{digest}.icc")
    for d in (OWN_DIR, ICC_DIR):
        for old in glob.glob(os.path.join(d, base + "-*.icc")):
            if os.path.basename(old) != os.path.basename(path):
                _forget(old)
    if not os.path.exists(path):
        with open(path, "wb") as f:
            f.write(data)
    return path


def _forget(path):
    if colord_available():
        rc, out, _ = run(["colormgr", "find-profile-by-filename", os.path.basename(path)])
        m = re.search(r"Object Path:\s*(\S+)", out)
        if m:
            run(["colormgr", "delete-profile", m.group(1)])
    try:
        os.remove(path)
    except OSError:
        pass


def apply(display, picture):
    """Install the profile for a Display. Returns an error string or None."""
    path = write_profile(display.key, display.name, display.edid, picture)
    if kde_available():
        rc, _, err = run(["kscreen-doctor", f"output.{display.connector}.iccprofile.{path}"])
        return err.strip() or None if rc else None
    if not colord_available():
        return "colord (colormgr) is not installed"
    dev = _colord_device(display.connector, display.edid.get("md5"))
    if not dev:
        return "this desktop has not registered the display with colord"
    rc, out, err = run(["colormgr", "find-profile-by-filename", os.path.basename(path)])
    m = re.search(r"Object Path:\s*(\S+)", out)
    if not m:
        rc, out, err = run(["colormgr", "import-profile", path])
        rc, out, err = run(["colormgr", "find-profile-by-filename", os.path.basename(path)])
        m = re.search(r"Object Path:\s*(\S+)", out)
    if not m:
        return f"colord did not accept the profile: {err.strip()}"
    run(["colormgr", "device-add-profile", dev, m.group(1)])
    rc, _, err = run(["colormgr", "device-make-profile-default", dev, m.group(1)])
    return err.strip() or None if rc else None
