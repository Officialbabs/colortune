"""X11: per-output gamma ramps (XRandR) and colour matrix / vibrance."""
import ctypes
import ctypes.util
import hashlib
import os
import re

from . import run, which

_ul = ctypes.c_ulong


class _ScreenResources(ctypes.Structure):
    _fields_ = [("timestamp", _ul), ("configTimestamp", _ul),
                ("ncrtc", ctypes.c_int), ("crtcs", ctypes.POINTER(_ul)),
                ("noutput", ctypes.c_int), ("outputs", ctypes.POINTER(_ul)),
                ("nmode", ctypes.c_int), ("modes", ctypes.c_void_p)]


class _OutputInfo(ctypes.Structure):
    _fields_ = [("timestamp", _ul), ("crtc", _ul), ("name", ctypes.c_char_p),
                ("nameLen", ctypes.c_int), ("mm_width", _ul), ("mm_height", _ul),
                ("connection", ctypes.c_ushort), ("subpixel_order", ctypes.c_ushort),
                ("ncrtc", ctypes.c_int), ("crtcs", ctypes.POINTER(_ul)),
                ("nclone", ctypes.c_int), ("clones", ctypes.POINTER(_ul)),
                ("nmode", ctypes.c_int), ("npreferred", ctypes.c_int), ("modes", ctypes.POINTER(_ul))]


class _Gamma(ctypes.Structure):
    _fields_ = [("size", ctypes.c_int), ("red", ctypes.POINTER(ctypes.c_ushort)),
                ("green", ctypes.POINTER(ctypes.c_ushort)), ("blue", ctypes.POINTER(ctypes.c_ushort))]


def is_x11_session():
    return os.environ.get("XDG_SESSION_TYPE") == "x11" or (
        bool(os.environ.get("DISPLAY")) and not os.environ.get("WAYLAND_DISPLAY"))


class XRandRGamma:
    def __init__(self):
        xlib = ctypes.util.find_library("X11")
        xrr = ctypes.util.find_library("Xrandr")
        if not (xlib and xrr):
            raise RuntimeError("libX11/libXrandr not found")
        self.x = ctypes.CDLL(xlib)
        self.r = ctypes.CDLL(xrr)
        self.x.XOpenDisplay.restype = ctypes.c_void_p
        self.x.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self.x.XDefaultRootWindow.restype = _ul
        self.x.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        self.x.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.r.XRRGetScreenResourcesCurrent.restype = ctypes.POINTER(_ScreenResources)
        self.r.XRRGetScreenResourcesCurrent.argtypes = [ctypes.c_void_p, _ul]
        self.r.XRRGetOutputInfo.restype = ctypes.POINTER(_OutputInfo)
        self.r.XRRGetOutputInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ScreenResources), _ul]
        self.r.XRRFreeOutputInfo.argtypes = [ctypes.POINTER(_OutputInfo)]
        self.r.XRRFreeScreenResources.argtypes = [ctypes.POINTER(_ScreenResources)]
        self.r.XRRGetCrtcGammaSize.argtypes = [ctypes.c_void_p, _ul]
        self.r.XRRAllocGamma.restype = ctypes.POINTER(_Gamma)
        self.r.XRRAllocGamma.argtypes = [ctypes.c_int]
        self.r.XRRSetCrtcGamma.argtypes = [ctypes.c_void_p, _ul, ctypes.POINTER(_Gamma)]
        self.r.XRRFreeGamma.argtypes = [ctypes.POINTER(_Gamma)]
        self.r.XRRGetCrtcGamma.restype = ctypes.POINTER(_Gamma)
        self.r.XRRGetCrtcGamma.argtypes = [ctypes.c_void_p, _ul]
        self.dpy = self.x.XOpenDisplay(None)
        if not self.dpy:
            raise RuntimeError("cannot open X display")

    def _crtcs(self):
        root = self.x.XDefaultRootWindow(self.dpy)
        res = self.r.XRRGetScreenResourcesCurrent(self.dpy, root)
        out = {}
        for i in range(res.contents.noutput):
            info = self.r.XRRGetOutputInfo(self.dpy, res, res.contents.outputs[i])
            if info.contents.crtc:
                out[info.contents.name.decode()] = info.contents.crtc
            self.r.XRRFreeOutputInfo(info)
        self.r.XRRFreeScreenResources(res)
        return out

    def outputs(self):
        return list(self._crtcs())

    def gamma_size(self, output):
        crtc = self._crtcs().get(output)
        return self.r.XRRGetCrtcGammaSize(self.dpy, crtc) if crtc else 0

    def get_ramps(self, output):
        """Current ramps (e.g. loaded by DisplayCAL/xcalib), so they can be restored."""
        crtc = self._crtcs().get(output)
        if not crtc:
            return None
        g = self.r.XRRGetCrtcGamma(self.dpy, crtc)
        if not g:
            return None
        n = g.contents.size
        out = [[g.contents.red[i] for i in range(n)], [g.contents.green[i] for i in range(n)],
               [g.contents.blue[i] for i in range(n)]]
        self.r.XRRFreeGamma(g)
        return out

    def set_ramps(self, output, ramps_u16):
        crtc = self._crtcs().get(output)
        if not crtc:
            return False
        size = self.r.XRRGetCrtcGammaSize(self.dpy, crtc)
        if size != len(ramps_u16[0]):
            raise ValueError("ramp size mismatch")
        g = self.r.XRRAllocGamma(size)
        for i in range(size):
            g.contents.red[i], g.contents.green[i], g.contents.blue[i] = (ch[i] for ch in ramps_u16)
        self.r.XRRSetCrtcGamma(self.dpy, crtc, g)
        self.r.XRRFreeGamma(g)
        self.x.XSync(self.dpy, 0)
        return True


def output_props():
    """{x11_output_name: {"edid_md5": str|None, "ctm": bool}} from `xrandr --verbose`."""
    if not which("xrandr"):
        return {}
    rc, out, _ = run(["xrandr", "--verbose"])
    props, cur, edid_lines, in_edid = {}, None, [], False
    for line in out.splitlines():
        m = re.match(r"^(\S+) (connected|disconnected)", line)
        if m:
            if cur and edid_lines:
                props[cur]["edid_md5"] = hashlib.md5(bytes.fromhex("".join(edid_lines))[:128]).hexdigest()
            cur, edid_lines, in_edid = m.group(1), [], False
            props[cur] = {"edid_md5": None, "ctm": False, "connected": m.group(2) == "connected"}
            continue
        if cur is None:
            continue
        s = line.strip()
        if s == "EDID:":
            in_edid = True
        elif in_edid and re.fullmatch(r"[0-9a-f]{32}", s):
            edid_lines.append(s)
        else:
            in_edid = False
            if s.startswith("CTM:"):
                props[cur]["ctm"] = True
    if cur and edid_lines:
        props[cur]["edid_md5"] = hashlib.md5(bytes.fromhex("".join(edid_lines))[:128]).hexdigest()
    return props


def _s31_32(v):
    mag = round(abs(v) * (1 << 32))
    if v < 0:
        mag |= 1 << 63
    return [mag & 0xFFFFFFFF, mag >> 32]


def set_ctm(output, matrix):
    vals = [w for row in matrix for v in row for w in _s31_32(v)]
    run(["xrandr", "--output", output, "--set", "CTM", ",".join(map(str, vals))])


def nvidia_available():
    return bool(which("nvidia-settings")) and os.path.exists("/proc/driver/nvidia/version")


def set_nvidia_vibrance(output, saturation):
    # DigitalVibrance -1024..1023, 0 = neutral
    v = max(-1024, min(1023, round((saturation - 1.0) * 1023)))
    run(["nvidia-settings", "-a", f"[DPY:{output}]/DigitalVibrance={v}"])
