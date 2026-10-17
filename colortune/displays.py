"""Find connected displays and work out which backends can drive each one."""
import glob
import os
import subprocess
import sys
import time

from . import config
from . import edid as edidmod
from .backends import WORKER, Control, backlight, ddc, icc, screenfilters, wlr, x11
from .colormath import DISPLAY_P3, srgb_coverage
from .config import DAEMON_SOCKET
from .edid import validate_primaries
from .lut import Picture, ramps_u16

STEPS = {
    "black": "Black level",
    "white": "White level",
    "gamma": "Gamma",
    "white_balance": "White balance",
    "saturation": "Saturation",
}


class Environment:
    """What this session lets us do, detected once."""

    def __init__(self):
        self.session = os.environ.get("XDG_SESSION_TYPE") or ("wayland" if os.environ.get("WAYLAND_DISPLAY") else "x11")
        self.desktop = os.environ.get("XDG_CURRENT_DESKTOP", "unknown")
        self.x11 = None
        self.x11_props = {}
        self.wlr = False
        if self.session == "x11" and os.environ.get("DISPLAY"):
            try:
                self.x11 = x11.XRandRGamma()
                self.x11_props = x11.output_props()
            except Exception as e:
                print(f"colortune: X11 gamma unavailable: {e}")
        elif self.session == "wayland":
            self.wlr = wlr.probe()
        self.kde = icc.kde_available()
        self.colord = icc.colord_available()
        self.gnome = icc.gnome_applies_vcgt()
        self.nvidia = x11.nvidia_available() and self.x11 is not None
        self.ddc = ddc.available()
        self.ada = screenfilters.ada_available()
        self.displays = []
        self.refresh_night_light()

    def refresh_night_light(self):
        self.night_light = screenfilters.night_light(gamma_tools=bool(self.x11 or self.wlr))
        return self.night_light

    @property
    def lut_method(self):
        if self.x11:
            return "x11"
        if self.wlr:
            return "wlr"
        if self.kde or (self.gnome and self.colord):
            return "icc"
        return None


LUT_LABELS = {
    "x11": "GPU gamma ramps (XRandR)",
    "wlr": "GPU gamma ramps (wlr-gamma-control)",
    "icc": "ICC calibration curves (colord / KWin)",
}


class Display:
    def __init__(self, env, connector, edid_bytes):
        self.env = env
        self.connector = connector
        self.edid = edidmod.parse(edid_bytes) if edid_bytes else {}
        self.key = edidmod.display_key(self.edid, connector)
        self.name = edidmod.pretty_name(self.edid, connector)
        self.internal = connector.startswith(backlight.INTERNAL_PREFIXES)
        if self.internal and not self.edid.get("name"):
            ident = f" ({self.edid['vendor']} {self.edid['product']:04X})" if self.edid else ""
            self.name = f"Built-in display{ident} — {connector}"
        self.backlight = None
        self.ddc_bus = None
        self.ddc_features = {}
        self.picture = Picture()
        self.hardware = {}
        self.x11_name = None
        self.lut = None
        self.matrix = None
        self.filter = None       # desktop colour filter used for saturation (Ada)
        self.profile_mode = "curves"
        self.ddc_preset = None
        self.last_error = None

    # ------------------------------------------------------------ setup
    def attach(self, ddc_list, probe_ddc=True):
        env = self.env
        if self.internal:
            name = backlight.find()
            if name:
                self.backlight = backlight.Backlight(name)
        for d in ddc_list:
            if d["connector"] == self.connector:
                self.ddc_bus = d["bus"]
        if self.ddc_bus is not None and probe_ddc:
            self.ddc_features = ddc.probe(self.ddc_bus)
            if any(c in self.ddc_features for c in (0x16, 0x18, 0x1A)):
                self.ddc_preset = ddc.get_preset(self.ddc_bus)

        if env.x11:
            names = env.x11.outputs()
            md5 = self.edid.get("md5")
            match = [n for n, p in env.x11_props.items() if md5 and p.get("edid_md5") == md5 and n in names]
            self.x11_name = match[0] if match else (self.connector if self.connector in names else None)
            if self.x11_name:
                self.lut = "x11"
                if env.nvidia:
                    self.matrix = "nvidia"
                elif env.x11_props.get(self.x11_name, {}).get("ctm"):
                    self.matrix = "ctm"
        elif env.lut_method:
            self.lut = env.lut_method
        if not self.matrix and env.ada:
            self.filter = "ada"

    def load(self, st, from_profile=False):
        if st:
            self.picture = Picture.from_dict(st.get("picture"))
            self.hardware = {k: float(v) for k, v in st.get("hardware", {}).items()
                             if not k.startswith("_")}
            self.profile_mode = st.get("profile_mode", self.profile_mode)
        if not from_profile:
            # The live system is the truth for things other tools also change.
            if self.backlight:
                self.hardware["backlight"] = self.backlight.get_percent()
            if self.filter == "ada":
                self.picture.saturation = screenfilters.ada_get_saturation()

    def to_state(self):
        return {"name": self.name, "picture": self.picture.to_dict(), "hardware": self.hardware,
                "profile_mode": self.profile_mode}

    # ------------------------------------------------------------ info
    def gamut(self):
        prim = self.edid.get("primaries")
        return srgb_coverage(prim) if prim else None

    def gamut_p3(self):
        prim = self.edid.get("primaries")
        return srgb_coverage(prim, DISPLAY_P3)[0] if prim else None

    def edid_problem(self):
        """Why the display's own colour data can't be trusted, or None."""
        return validate_primaries(self.edid.get("primaries")) if self.edid else "no display data"

    def ada_saturation_status(self):
        return screenfilters.ada_saturation_status() if self.filter == "ada" else None

    def saturation_method(self):
        if self.matrix == "nvidia":
            return "NVIDIA digital vibrance"
        if self.matrix == "ctm":
            return "GPU colour matrix (CTM)"
        if 0x8A in self.ddc_features:
            return "Monitor saturation (DDC/CI)"
        if self.filter == "ada":
            if self.ada_saturation_status() == "theirs":
                return "Ada's colour filter is already in use. Turn it off in Settings → Colour filters to use this"
            return "Ada colour filter: can lower saturation, not raise it"
        return None

    def methods(self):
        """[(label, available, detail)] for the UI."""
        rows = []
        if self.internal:
            rows.append(("Backlight", self.backlight is not None,
                         self.backlight.name if self.backlight else "no backlight device"))
        else:
            if not self.env.ddc:
                detail = "install ddcutil to control the monitor itself"
            elif self.ddc_bus is None:
                detail = "monitor did not answer (enable DDC/CI in its menu; some docks/adapters block it)"
            else:
                detail = f"i2c-{self.ddc_bus}: " + (", ".join(ddc.FEATURES[c][0] for c in self.ddc_features) or "no adjustable features")
            rows.append(("Monitor controls (DDC/CI)", self.ddc_bus is not None and bool(self.ddc_features), detail))
        rows.append(("Picture curves", self.lut is not None,
                     LUT_LABELS.get(self.lut, "this desktop doesn't let apps change colour curves")))
        sat = self.saturation_method()
        rows.append(("Saturation", sat is not None and self.ada_saturation_status() != "theirs",
                     sat or "needs X11 with a colour-matrix driver, NVIDIA, or a monitor with DDC saturation"))
        return rows

    # ------------------------------------------------------------ controls
    def controls(self):
        cs = []
        if self.backlight:
            cs.append(Control("backlight", "Backlight", "hardware", 1, 100, self.backlight.get_percent(),
                              self._set_backlight, step=1, default=None, steps=("black", "white"),
                              tip="Lower backlight = deeper blacks on LCD (and less glow)", source="backlight"))
        for code, (cur, mx) in self.ddc_features.items():
            label, steps, tip = ddc.FEATURES[code]
            cs.append(Control(f"vcp{code:02X}", label, "hardware", 0, mx,
                              self.hardware.get(f"vcp{code:02X}", cur),
                              lambda v, c=code: self._set_vcp(c, v), steps=steps, tip=tip, source="ddc"))
        P = Picture
        spec = []
        if self.lut:
            spec += [
                ("black", "Black point", -0.10, 0.10, 0.005, 3, ("black",),
                 "Below 0 hides the faint grey glow for deeper blacks; above 0 reveals shadow detail"),
                ("gamma", "Gamma", 0.6, 1.6, 0.01, 2, ("gamma",), "Above 1 brightens mid-tones, below 1 deepens them"),
                ("contrast", "Contrast", 0.85, 1.25, 0.01, 2, ("white",), ""),
                ("brightness", "White level", 0.5, 1.0, 0.01, 2, ("white",), "Dims whites without touching the backlight"),
                ("red", "Red", 0.5, 1.0, 0.005, 3, ("white_balance",), "Lower the colour that tints your greys"),
                ("green", "Green", 0.5, 1.0, 0.005, 3, ("white_balance",), ""),
                ("blue", "Blue", 0.5, 1.0, 0.005, 3, ("white_balance",), ""),
            ]
        if self.matrix:
            spec.append(("saturation", "Saturation", 0.0, 2.0, 0.01, 2, ("saturation",),
                         "100% is natural. Higher is punchier but less accurate"))
        elif self.filter == "ada" and self.ada_saturation_status() != "theirs":
            spec.append(("saturation", "Saturation", 0.0, 1.0, 0.1, 1, ("saturation",),
                         "100% leaves colours untouched. Lower mutes them. Ada can lower saturation "
                         "but not raise it"))
        for key, label, lo, hi, step, digits, steps, tip in spec:
            cs.append(Control(key, label, "picture", lo, hi, getattr(self.picture, key),
                              lambda v, k=key: self._set_picture(k, v), step=step, digits=digits,
                              default=P.__dataclass_fields__[key].default, steps=steps, tip=tip,
                              source="ada" if key == "saturation" and not self.matrix else self.lut))
        return cs

    # ------------------------------------------------------------ setters
    def _set_backlight(self, v):
        self.hardware["backlight"] = v
        WORKER.submit(f"{self.key}:backlight", self.backlight.set_percent, v)

    def _set_vcp(self, code, v):
        self.hardware[f"vcp{code:02X}"] = v
        WORKER.submit(f"{self.key}:vcp{code:02X}", lambda val: ddc.set_vcp(self.ddc_bus, code, val), v)

    def set_preset(self, code):
        """Monitor colour mode. RGB gains usually only work in 'User'; sRGB is the
        monitor's own accurate mode. Only changed when the user asks."""
        self.ddc_preset = code
        WORKER.submit(f"{self.key}:preset", lambda c: ddc.set_preset(self.ddc_bus, c), code)

    def _set_picture(self, key, v):
        if key == "saturation" and not self.matrix and self.filter == "ada":
            v = screenfilters.snap_saturation(v)
            for d in self.env.displays:  # one filter for the whole desktop
                if d.filter == "ada":
                    d.picture.saturation = v
            WORKER.submit("ada:saturation", self._apply_ada_now, v)
            return v
        setattr(self.picture, key, float(v))
        self.apply_picture()
        return v

    def _apply_ada_now(self, v):
        if screenfilters.ada_set_saturation(v) is None:
            self.last_error = ("Saturation not changed: Ada's colour filter is in use for another filter "
                               "(Settings → Colour filters)")
        else:
            self.last_error = None

    def apply_picture(self):
        pic = Picture.from_dict(self.picture.to_dict())
        WORKER.submit(f"{self.key}:picture", self._apply_picture_now, pic)

    def _x11_backup(self):
        """Remember the ramps that were loaded before our first change (DisplayCAL, xcalib...)."""
        def update(state):
            orig = state.setdefault("x11_original", {})
            if self.key not in orig:
                orig[self.key] = self.env.x11.get_ramps(self.x11_name)
        config.update_state(update)

    def _apply_picture_now(self, pic, restore=False):
        err = None
        if self.lut == "x11":
            if restore or pic.lut_identity():
                # Neutral curves = whatever was loaded before ColorTune (DisplayCAL, xcalib...).
                # No backup means ColorTune never changed the ramps: leave them alone.
                orig = config.load_state().get("x11_original", {}).get(self.key)
                size = self.env.x11.gamma_size(self.x11_name)
                if orig and size and len(orig[0]) == size:
                    self.env.x11.set_ramps(self.x11_name, orig)
                config.update_state(lambda st: st.get("x11_original", {}).pop(self.key, None))
            else:
                self._x11_backup()
                size = self.env.x11.gamma_size(self.x11_name)
                if size:
                    self.env.x11.set_ramps(self.x11_name, ramps_u16(pic, size))
            if self.matrix == "ctm":
                from .lut import ctm
                x11.set_ctm(self.x11_name, ctm(pic))
            elif self.matrix == "nvidia":
                x11.set_nvidia_vibrance(self.x11_name, pic.saturation)
        elif self.lut == "wlr":
            msg = {"output": self.connector, "picture": pic.to_dict()}
            reply = wlr.send(msg)
            if reply is None and not pic.lut_identity():
                ensure_daemon()
                reply = wlr.send(msg)
            err = (reply or {}).get("error") if reply else None
            if reply is None and not pic.lut_identity():
                err = "could not reach the colortune daemon"
        elif self.lut == "icc":
            err = icc.restore(self) if restore else icc.apply(self, pic, self.profile_mode)
        self.last_error = err
        if err:
            print(f"colortune: {self.connector}: {err}")

    def picture_changed(self):
        return not self.picture.lut_identity() or (bool(self.matrix) and self.picture.saturation != 1.0)

    def set_profile_mode(self, mode):
        self.profile_mode = mode
        if self.lut == "icc" and self.picture_changed():
            self.apply_picture()

    def apply_all(self, picture=True, backlight=True, force=False):
        """Re-apply what is stored. Displays the user never adjusted are left alone,
        so calibration from other tools (DisplayCAL, desktop profiles) survives.
        force (profiles): a neutral picture actively puts back what was there before.
        Ada saturation is desktop-wide: see apply_desktop_saturation()."""
        for c in self.controls():
            if c.group == "hardware" and c.key in self.hardware and (backlight or c.key != "backlight"):
                c.setter(self.hardware[c.key])
        if self.lut and picture:
            if self.picture_changed():
                self.apply_picture()
            elif force and self.touched():
                self._queue_restore()

    def touched(self):
        """Has ColorTune changed this display's curves or profile (now or in a backup)?"""
        st = config.load_state()
        return (self.picture_changed() or self.key in st.get("x11_original", {})
                or self.key in st.get("icc_previous", {}) or self.lut == "wlr")

    def _queue_restore(self):
        WORKER.submit(f"{self.key}:picture", lambda p: self._apply_picture_now(p, restore=True), Picture())

    def reset(self):
        """Back to how the display was before ColorTune: previous curves/profile restored.
        Displays ColorTune never changed are not touched at all."""
        sat = self.picture.saturation
        touched = self.lut and self.touched()
        self.picture = Picture()
        if touched:
            self._queue_restore()
        if self.filter == "ada" and sat != 1.0:
            self._set_picture("saturation", 1.0)


def apply_desktop_saturation(displays):
    """Write Ada's desktop-wide filter once, from the first Ada display's value - and
    only if ColorTune owns the filter or it is off."""
    ada = [d for d in displays if d.filter == "ada"]
    if not ada:
        return
    status = screenfilters.ada_saturation_status()
    v = ada[0].picture.saturation
    if status == "theirs" or (status == "free" and v >= 1.0):
        return
    ada[0]._set_picture("saturation", v)


def ensure_daemon(timeout=3.0):
    if wlr.send({"cmd": "ping"}, timeout=0.5) is not None:
        return True
    pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ, PYTHONPATH=pkg_parent + os.pathsep + os.environ.get("PYTHONPATH", ""))
    subprocess.Popen([sys.executable, "-m", "colortune", "daemon"], env=env, start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    end = time.time() + timeout
    while time.time() < end:
        if os.path.exists(DAEMON_SOCKET):
            return True
        time.sleep(0.05)
    return False


def _drm_connectors():
    out = []
    for path in sorted(glob.glob("/sys/class/drm/card*-*")):
        try:
            with open(os.path.join(path, "status")) as f:
                if f.read().strip() != "connected":
                    continue
        except OSError:
            continue
        try:
            with open(os.path.join(path, "edid"), "rb") as f:
                data = f.read()
        except OSError:
            data = b""
        out.append((os.path.basename(path).split("-", 1)[1], data))
    return out


def discover(env=None, probe_ddc=True, state=None):
    env = env or Environment()
    ddc_list = ddc.detect() if (env.ddc and probe_ddc) else []
    conns = _drm_connectors()
    if not conns and env.x11:  # e.g. NVIDIA without DRM modesetting
        conns = [(n, b"") for n in env.x11.outputs()]
    displays = []
    for connector, data in conns:
        d = Display(env, connector, data)
        d.attach(ddc_list, probe_ddc)
        d.load((state or {}).get("displays", {}).get(d.key))
        displays.append(d)
    displays.sort(key=lambda d: (not d.internal, d.connector))
    env.displays = displays
    return env, displays
