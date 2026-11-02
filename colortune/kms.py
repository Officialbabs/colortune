#!/usr/bin/python3 -I
"""Saturation through the graphics chip's colour matrix (the KMS "CTM").

The display pipeline of nearly every GPU has a 3x3 colour matrix that is applied after
the desktop has drawn the screen, at no cost. Only the program in charge of the screen
(the DRM master, normally the compositor) can set it, and it stays set until something
changes it. Desktops with no colour API of their own (Ada, for one) never touch it, so
ColorTune sets it at startup, just before the sign-in screen takes the display, and it
lasts the whole session. To change it while the desktop runs, `set` steps aside to a spare
text console for a moment (the desktop lets go of the screen), sets it and comes back.

This file runs as root, so it uses only the standard library and libdrm. `install` copies
it to a root-owned file, links it as /usr/local/sbin/colortune-ctm and adds a polkit action
so the app can run it with pkexec. Commands:

    show [S]          the matrix for saturation S as the hardware will apply it
    status            what the screens and the startup setting are doing now
    try S             text console only: switch between normal and S for a few seconds
    set S | off       apply now; from a desktop the screen blanks for a moment (root)
    startup S | off   also set it at every startup, or stop (root)
    install [S]       install the helper; with S, also turn on startup (root)
    uninstall         remove the helper and the startup step (root)
    apply             what startup runs

Add colortune.ctm=0 to the kernel command line to skip it for one boot.
"""
import argparse
import ctypes
import ctypes.util
import errno
import fcntl
import glob
import os
import re
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import time

LIBEXEC = "/usr/local/libexec/colortune-ctm"
COMMAND = "/usr/local/sbin/colortune-ctm"  # so `sudo colortune-ctm` works
CONF = "/etc/colortune/saturation"
DROPIN = "colortune-ctm.conf"
UNIT_DIR = "/etc/systemd/system"
POLICY = "/usr/share/polkit-1/actions/ai.stuard.colortune.ctm.policy"
MIN_S, MAX_S = 0.5, 1.5
REC709 = (0.2126, 0.7152, 0.0722)
SAMPLES = {"skin": "e0aa87", "sky": "64a5e1", "grass": "5fa03c",  # for `try`
           "brick": "b9503c", "teal": "289691", "purple": "8c5fb9"}
ONE = 1 << 32  # 1.0 in S31.32

OBJ_CRTC = 0xcccccccc
CAP_ATOMIC = 3
TEST_ONLY, ALLOW_MODESET = 0x0100, 0x0400
KDGETMODE, KD_TEXT = 0x4B3B, 0
VT_GETMODE, VT_SETMODE, VT_RELDISP, VT_AUTO, VT_PROCESS = 0x5601, 0x5602, 0x5605, 0, 1
VT_GETSTATE, VT_ACTIVATE, VT_DISALLOCATE = 0x5603, 0x5606, 0x5608
SPARE_VTS = range(8, 16)  # above the consoles logind starts sign-in prompts on (1-6)

POLICY_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE policyconfig PUBLIC "-//freedesktop//DTD PolicyKit Policy Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/PolicyKit/1/policyconfig.dtd">
<policyconfig>
  <vendor>ColorTune</vendor>
  <action id="ai.stuard.colortune.ctm">
    <description>Change the screen's colour boost</description>
    <message>Authentication is required to change the screen's colour boost</message>
    <defaults>
      <allow_any>auth_admin</allow_any>
      <allow_inactive>auth_admin</allow_inactive>
      <allow_active>auth_admin_keep</allow_active>
    </defaults>
    <annotate key="org.freedesktop.policykit.exec.path">{LIBEXEC}</annotate>
  </action>
</policyconfig>
"""


# ---------------------------------------------------------------- matrix maths

def matrix(s, w=REC709):
    """s*I + (1-s)*[w;w;w]: greys keep their value, colours move away from (or towards) them."""
    return [[(s if i == j else 0.0) + (1 - s) * w[j] for j in range(3)] for i in range(3)]


def _i915_bits(a):
    for limit, fbits in ((ONE >> 3, 12), (ONE >> 2, 11), (ONE >> 1, 10), (ONE, 9), (2 * ONE, 8)):
        if a < limit:
            return fbits
    return 7


def i915(c):
    """The coefficient Intel hardware applies for c (S31.32 units, signed): a 9-bit
    mantissa whose scale depends on the size, limited to |c| < 4 (intel_color.c)."""
    a = min(abs(c), 4 * ONE - 1)
    fbits = _i915_bits(a)
    m = (min((a >> (32 - fbits - 3)) + 4, 0xfff) & 0xff8) >> 3
    v = m << (32 - fbits)
    return -v if c < 0 else v


QUANTISERS = {"i915": i915, "xe": i915}


def fit(s, driver=None):
    """The matrix for saturation s as 3 rows of signed S31.32 integers the driver can hold
    exactly, each row summing to exactly 1 so whites and greys stay neutral. (Intel chips
    before Ice Lake rescale it again for limited-range output, off by up to one level.)"""
    q = QUANTISERS.get(driver)
    rows = []
    for i, row in enumerate(matrix(s)):
        target = [round(v * ONE) for v in row]
        if q is None:
            off = [target[j] if j != i else 0 for j in range(3)]
            rows.append([ONE - sum(off) if j == i else off[j] for j in range(3)])
        else:
            rows.append(_fit_row(target, q))
    return rows


def _fit_row(target, q, n=8):
    near = []
    for t in target:
        step = 1 << (32 - _i915_bits(min(abs(t), 4 * ONE - 1)))
        near.append(sorted({q(t + k * step) for k in range(-n, n + 1)}))
    best = None
    for i in range(3):
        j, k = [x for x in range(3) if x != i]
        for a in near[j]:
            for b in near[k]:
                c = ONE - a - b
                if q(c) != c:
                    continue
                row = [0, 0, 0]
                row[i], row[j], row[k] = c, a, b
                err = max(abs(row[x] - target[x]) for x in range(3))
                if best is None or err < best[0]:
                    best = (err, row)
    return best[1] if best else [q(t) for t in target]


def pack(rows):
    """72 bytes of struct drm_color_ctm: sign-magnitude S31.32, row by row."""
    return struct.pack("=9Q", *((1 << 63) | -c if c < 0 else c for row in rows for c in row))


def unpack(data):
    vals = [(-(v & ((1 << 63) - 1)) if v >> 63 else v) / ONE for v in struct.unpack("=9Q", data[:72])]
    return [vals[0:3], vals[3:6], vals[6:9]]


def saturation_of(m, tolerance=0.004):
    """The saturation a matrix applies, if it is one of ours; otherwise None."""
    s = (m[0][0] + m[1][1] + m[2][2] - 1) / 2
    ref = matrix(s)
    if all(abs(m[i][j] - ref[i][j]) < tolerance for i in range(3) for j in range(3)):
        return round(s, 2)
    return None


def valid(text):
    try:
        s = round(float(text), 2)
    except (TypeError, ValueError):
        return None
    return s if MIN_S <= s <= MAX_S else None


def read_conf(path=CONF):
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except (OSError, UnicodeError):
        return None
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if line:
            return valid(line)
    return None


def skipped(cmdline="/proc/cmdline"):
    try:
        with open(cmdline) as f:
            return "colortune.ctm=0" in f.read().split()
    except OSError:
        return False


# ---------------------------------------------------------------- libdrm

class _Version(ctypes.Structure):
    _fields_ = [("major", ctypes.c_int), ("minor", ctypes.c_int), ("patch", ctypes.c_int),
                ("name_len", ctypes.c_int), ("name", ctypes.c_char_p),
                ("date_len", ctypes.c_int), ("date", ctypes.c_char_p),
                ("desc_len", ctypes.c_int), ("desc", ctypes.c_char_p)]


class _Res(ctypes.Structure):
    _fields_ = [("count_fbs", ctypes.c_int), ("fbs", ctypes.POINTER(ctypes.c_uint32)),
                ("count_crtcs", ctypes.c_int), ("crtcs", ctypes.POINTER(ctypes.c_uint32)),
                ("count_connectors", ctypes.c_int), ("connectors", ctypes.POINTER(ctypes.c_uint32)),
                ("count_encoders", ctypes.c_int), ("encoders", ctypes.POINTER(ctypes.c_uint32)),
                ("min_width", ctypes.c_uint32), ("max_width", ctypes.c_uint32),
                ("min_height", ctypes.c_uint32), ("max_height", ctypes.c_uint32)]


class _ObjProps(ctypes.Structure):
    _fields_ = [("count_props", ctypes.c_uint32), ("props", ctypes.POINTER(ctypes.c_uint32)),
                ("prop_values", ctypes.POINTER(ctypes.c_uint64))]


class _Prop(ctypes.Structure):
    _fields_ = [("prop_id", ctypes.c_uint32), ("flags", ctypes.c_uint32), ("name", ctypes.c_char * 32),
                ("count_values", ctypes.c_int), ("values", ctypes.POINTER(ctypes.c_uint64)),
                ("count_enums", ctypes.c_int), ("enums", ctypes.c_void_p),
                ("count_blobs", ctypes.c_int), ("blob_ids", ctypes.POINTER(ctypes.c_uint32))]


class _Blob(ctypes.Structure):
    _fields_ = [("id", ctypes.c_uint32), ("length", ctypes.c_uint32), ("data", ctypes.c_void_p)]


_LIB = None


def libdrm():
    global _LIB
    if _LIB is None:
        try:
            lib = ctypes.CDLL("libdrm.so.2", use_errno=True)
        except OSError:
            lib = ctypes.CDLL(ctypes.util.find_library("drm") or "libdrm.so.2", use_errno=True)
        P, I, U32, U64, V = ctypes.POINTER, ctypes.c_int, ctypes.c_uint32, ctypes.c_uint64, ctypes.c_void_p
        for name, res, args in (
                ("drmGetVersion", P(_Version), [I]), ("drmFreeVersion", None, [P(_Version)]),
                ("drmIsMaster", I, [I]), ("drmSetMaster", I, [I]), ("drmDropMaster", I, [I]),
                ("drmSetClientCap", I, [I, U64, U64]),
                ("drmModeGetResources", P(_Res), [I]), ("drmModeFreeResources", None, [P(_Res)]),
                ("drmModeObjectGetProperties", P(_ObjProps), [I, U32, U32]),
                ("drmModeFreeObjectProperties", None, [P(_ObjProps)]),
                ("drmModeGetProperty", P(_Prop), [I, U32]), ("drmModeFreeProperty", None, [P(_Prop)]),
                ("drmModeGetPropertyBlob", P(_Blob), [I, U32]), ("drmModeFreePropertyBlob", None, [P(_Blob)]),
                ("drmModeCreatePropertyBlob", I, [I, V, ctypes.c_size_t, P(U32)]),
                ("drmModeDestroyPropertyBlob", I, [I, U32]),
                ("drmModeAtomicAlloc", V, []), ("drmModeAtomicFree", None, [V]),
                ("drmModeAtomicAddProperty", I, [V, U32, U32, U64]),
                ("drmModeAtomicCommit", I, [I, V, U32, V])):
            fn = getattr(lib, name)
            fn.restype, fn.argtypes = res, args
        _LIB = lib
    return _LIB


class Crtc:
    def __init__(self, crtc_id, ctm_prop, ctm_blob, active):
        self.id, self.ctm_prop, self.ctm_blob, self.active = crtc_id, ctm_prop, ctm_blob, active


class Card:
    """One /dev/dri/card*, with the CRTCs (display pipes) that have a colour matrix."""

    def __init__(self, path, read_only=False):
        self.path = path
        self.lib = libdrm()
        self.fd = os.open(path, os.O_RDWR | os.O_CLOEXEC)
        if read_only and self.lib.drmIsMaster(self.fd):
            # Nobody was in charge of the screen (a desktop is just getting it back), so
            # opening made us: give it up at once, or the desktop can't take it.
            self.lib.drmDropMaster(self.fd)
        try:
            v = self.lib.drmGetVersion(self.fd)
            self.driver = v.contents.name.decode(errors="replace") if v else "unknown"
            if v:
                self.lib.drmFreeVersion(v)
            self.lib.drmSetClientCap(self.fd, CAP_ATOMIC, 1)
            self.crtcs = self._crtcs()
        except BaseException:
            os.close(self.fd)
            raise

    def _crtcs(self):
        lib, out = self.lib, []
        res = lib.drmModeGetResources(self.fd)
        if not res:
            return out
        try:
            for n in range(res.contents.count_crtcs):
                crtc = res.contents.crtcs[n]
                props = lib.drmModeObjectGetProperties(self.fd, crtc, OBJ_CRTC)
                if not props:
                    continue
                found = {}
                for k in range(props.contents.count_props):
                    p = lib.drmModeGetProperty(self.fd, props.contents.props[k])
                    if p:
                        found[p.contents.name.decode(errors="replace")] = (p.contents.prop_id,
                                                                            props.contents.prop_values[k])
                        lib.drmModeFreeProperty(p)
                lib.drmModeFreeObjectProperties(props)
                if "CTM" in found:
                    out.append(Crtc(crtc, found["CTM"][0], found["CTM"][1], bool(found.get("ACTIVE", (0, 0))[1])))
        finally:
            lib.drmModeFreeResources(res)
        return out

    def close(self):
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def master(self):
        return bool(self.lib.drmIsMaster(self.fd))

    def read_raw(self, blob_id):
        """The 72 bytes of a CTM blob, or None."""
        if not blob_id:
            return None
        b = self.lib.drmModeGetPropertyBlob(self.fd, blob_id)
        if not b:
            return None
        try:
            return ctypes.string_at(b.contents.data, 72) if b.contents.length >= 72 else None
        finally:
            self.lib.drmModeFreePropertyBlob(b)

    def read(self, blob_id):
        """The matrix in a CTM blob, or None."""
        data = self.read_raw(blob_id)
        return unpack(data) if data else None

    def keep(self, blob_id):
        """A copy of a pipe's blob owned by this fd (0 for none). The kernel frees the
        original, and its id, as soon as no pipe uses it, so it can't be put back by id."""
        if not blob_id:
            return 0
        data = self.read_raw(blob_id)
        if data is None:
            raise OSError(errno.ENOENT, "could not read the current colour matrix")
        return self.create(data)

    def create(self, data):
        """A CTM blob from 72 packed bytes; this fd owns it until destroy() or close()."""
        blob = ctypes.c_uint32()
        rc = self.lib.drmModeCreatePropertyBlob(self.fd, data, len(data), ctypes.byref(blob))
        if rc:
            raise OSError(-rc, os.strerror(-rc))
        return blob.value

    def destroy(self, blob_id):
        if blob_id:
            self.lib.drmModeDestroyPropertyBlob(self.fd, blob_id)

    def commit(self, values):
        """Set CTM blobs {crtc_id: blob_id, 0 for none} in one atomic commit, checked first.
        Returns None, or what went wrong."""
        lib = self.lib
        req = lib.drmModeAtomicAlloc()
        if not req:
            return "out of memory"
        try:
            for crtc in self.crtcs:
                if crtc.id in values and lib.drmModeAtomicAddProperty(req, crtc.id, crtc.ctm_prop,
                                                                      values[crtc.id]) < 0:
                    return "could not build the request"
            rc = 0
            for flags in (0, ALLOW_MODESET):
                rc = lib.drmModeAtomicCommit(self.fd, req, flags | TEST_ONLY, None)
                if rc == 0:
                    rc = lib.drmModeAtomicCommit(self.fd, req, flags, None)
                    if rc == 0:
                        for crtc in self.crtcs:
                            crtc.ctm_blob = values.get(crtc.id, crtc.ctm_blob)
                        return None
                if rc != -errno.EINVAL:
                    break
            return os.strerror(-rc)
        finally:
            lib.drmModeAtomicFree(req)


def cards(read_only=False):
    """Every card with a colour matrix that can be opened: [Card]."""
    out = []
    for path in sorted(glob.glob("/dev/dri/card[0-9]*")):
        try:
            card = Card(path, read_only)
        except OSError:
            continue
        if card.crtcs:
            out.append(card)
        else:
            card.close()
    return out


def drivers():
    """Driver names of the display cards, from sysfs. Opening a card on a text console can
    put us in charge of the screen, so read-only commands avoid it when they can."""
    out = []
    for d in sorted(glob.glob("/sys/class/drm/card[0-9]*")):
        if "-" not in os.path.basename(d):  # not a connector such as card1-eDP-1
            try:
                out.append(os.path.basename(os.readlink(os.path.join(d, "device", "driver"))))
            except OSError:
                pass
    return out


def set_saturation(card, s):
    """Apply s to every pipe of a card we are master of. Returns None or an error."""
    blob = card.create(pack(fit(s, card.driver))) if s != 1.0 else 0
    try:
        return card.commit({c.id: blob for c in card.crtcs})
    finally:
        card.destroy(blob)  # the pipes keep their own reference


class Console:
    """While in effect, a switch away from this text console waits until we have let go of
    the screen. Otherwise the desktop can come back while we are in charge of the screen,
    fail to take it over, and stay frozen. The kernel drops this if we die (VT_PROCESS)."""

    STOPS = (signal.SIGTSTP, signal.SIGTTIN, signal.SIGTTOU)  # a stopped owner would block switching

    def __init__(self, vt=None):
        """vt: the console to guard; by default the one on screen. A console that isn't on
        screen yet can be guarded too, before switching to it."""
        self.vt = vt
        self.fd, self.mode, self.text, self.leaving = -1, None, False, False
        self._saved = {}

    def __enter__(self):
        try:
            if self.vt is None:
                with open("/sys/class/tty/tty0/active") as f:
                    name = f.read().strip()
            else:
                name = f"tty{self.vt}"
            fd = os.open("/dev/" + name, os.O_RDWR | os.O_NOCTTY | os.O_CLOEXEC)
        except OSError:
            return self  # no virtual consoles
        try:
            self.text = struct.unpack("i", fcntl.ioctl(fd, KDGETMODE, bytes(4)))[0] == KD_TEXT
            if not self.text:
                raise OSError  # a desktop's console: the desktop handles switching
            mode = fcntl.ioctl(fd, VT_GETMODE, bytes(8))
            self._set(signal.SIGUSR1, self._release)
            self._set(signal.SIGUSR2, signal.SIG_IGN)
            for sig in self.STOPS:
                self._set(sig, signal.SIG_IGN)
            # A text console already in VT_PROCESS was left that way by a run that died.
            fcntl.ioctl(fd, VT_SETMODE, struct.pack("bbhhh", VT_PROCESS, 0, signal.SIGUSR1, signal.SIGUSR2, 0))
        except OSError:
            self._restore_signals()
            os.close(fd)
            return self
        self.fd = fd
        self.mode = mode if mode[0] == VT_AUTO else struct.pack("bbhhh", VT_AUTO, 0, 0, 0, 0)
        return self

    def _set(self, sig, handler):
        old = signal.signal(sig, handler)
        self._saved.setdefault(sig, signal.SIG_DFL if old is None else old)

    def _restore_signals(self):
        for sig, handler in self._saved.items():
            signal.signal(sig, handler)
        self._saved.clear()

    def _release(self, *_):
        self.leaving = True

    def hold_interrupts(self):
        """From here on Ctrl+C can't stop us half way; it comes back after __exit__."""
        self._set(signal.SIGINT, signal.SIG_IGN)

    def wait(self, seconds):
        """Sleep, but stop as soon as someone wants to switch away."""
        end = time.monotonic() + seconds
        while not self.leaving and time.monotonic() < end:
            time.sleep(0.05)

    def write(self, text):
        """Control text for the console. Written to it directly when we hold it, the console
        acts on it before this returns; through stdout (and sudo's pty) it can come late."""
        if self.fd >= 0:
            os.write(self.fd, text.encode())
        else:
            print(text, end="", flush=True)
            self.wait(0.3)

    def __exit__(self, *exc):
        if self.fd >= 0:
            try:
                if self.leaving:
                    fcntl.ioctl(self.fd, VT_RELDISP, 1)  # let the waiting switch go ahead
                fcntl.ioctl(self.fd, VT_SETMODE, self.mode)
            except OSError:
                pass
            os.close(self.fd)
            self.fd = -1
        self._restore_signals()
        return False


def palette_codes():
    """Linux console escapes putting the everyday SAMPLES colours in palette slots 9-14."""
    return "".join(f"\033]P{9 + i:X}{rgb}" for i, rgb in enumerate(SAMPLES.values()))


NO_GUARD = ("could not hold console switching while changing the screen, so nothing was\n"
            "changed. Switch to another console and back (Ctrl+Alt+F4, Ctrl+Alt+F3), then try again.")


def active_vt():
    """Number of the console on screen, or None."""
    try:
        with open("/sys/class/tty/tty0/active") as f:
            name = f.read().strip()
    except OSError:
        return None
    return int(name[3:]) if name.startswith("tty") and name[3:].isdigit() else None


def _vt_ioctl(path, request, arg):
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_CLOEXEC)
    try:
        return fcntl.ioctl(fd, request, arg)
    finally:
        os.close(fd)


def on_text_console():
    n = active_vt()
    try:
        return n is not None and struct.unpack("i", _vt_ioctl(f"/dev/tty{n}", KDGETMODE, bytes(4)))[0] == KD_TEXT
    except OSError:
        return False


def _spare_vt():
    """A text console nobody has open, above the ones that get sign-in prompts."""
    try:
        in_use = struct.unpack("HHH", _vt_ioctl("/dev/tty0", VT_GETSTATE, bytes(6)))[2]
    except OSError:
        return None
    for n in SPARE_VTS:
        if in_use & (1 << n):
            continue
        try:  # one a dead program left in graphics or process mode couldn't be left again
            text = struct.unpack("i", _vt_ioctl(f"/dev/tty{n}", KDGETMODE, bytes(4)))[0] == KD_TEXT
            auto = _vt_ioctl(f"/dev/tty{n}", VT_GETMODE, bytes(8))[0] == VT_AUTO
        except OSError:
            continue
        if text and auto:
            return n
    return None


def _vt_write(n, text):
    fd = os.open(f"/dev/tty{n}", os.O_WRONLY | os.O_NOCTTY | os.O_CLOEXEC)
    try:
        os.write(fd, text.encode())
    finally:
        os.close(fd)


def _master_tgids():
    """{minor: process id} of whoever is in charge of each graphics device now, from
    debugfs (root only), or None when that can't be read. A desktop's device shows logind,
    which opened it for the desktop."""
    out, read = {}, False
    for path in glob.glob("/sys/kernel/debug/dri/*/clients"):
        try:
            with open(path) as f:
                lines = f.read().splitlines()
        except OSError:
            continue
        read = True
        for line in lines:  # command  tgid  dev  master  a  uid  magic  [name  id]
            m = re.search(r"\s(\d+)\s+(\d+)\s+([yn])\s+[yn]\s+\d+\s+\d+", line)
            if m and m.group(3) == "y":
                out[int(m.group(2))] = int(m.group(1))
    return out if read else None


def _comm(pid):
    try:
        with open(f"/proc/{pid}/comm") as f:
            return f"{f.read().strip()} ({pid})"
    except OSError:
        return str(pid)


def _session_active(vt):
    for path in glob.glob("/run/systemd/sessions/*"):
        try:
            with open(path) as f:
                fields = dict(line.split("=", 1) for line in f.read().splitlines() if "=" in line)
        except (OSError, UnicodeError):
            continue
        if fields.get("VTNR") == str(vt) and fields.get("ACTIVE") == "1":
            return True
    return False


def _desktop_back(vt, before, timeout=20.0):
    """After returning to the desktop's console vt, wait until the desktop is in charge of
    the screen again: the same process in charge of each device as before we stepped aside
    (before, from _master_tgids). Until logind hands it back nobody is, and whatever opened
    the card first (even ColorTune's own window) would take it and leave the desktop black.
    Returns None once it's back, else who has the screen instead ("" for nobody): only once
    logind has had its go (it marks the session active just before), or after timeout."""
    end = time.monotonic() + timeout
    if before is None:  # no debugfs: logind marks the session active just before it hands back
        try:
            subprocess.run(["udevadm", "settle", f"--timeout={int(timeout)}"], capture_output=True,
                           timeout=timeout + 1)
        except (OSError, subprocess.TimeoutExpired):
            pass
        while not _session_active(vt):
            if time.monotonic() > end:
                return ""
            time.sleep(0.05)
        time.sleep(0.3)
        return None
    tried = None
    while True:
        now = _master_tgids() or {}
        if all(now.get(m) == pid for m, pid in before.items()):
            return None
        if tried is None and _session_active(vt):
            tried = time.monotonic()
        if time.monotonic() > end or (tried is not None and time.monotonic() - tried > 2.0):
            return ", ".join(sorted({_comm(pid) for m, pid in now.items() if m in before and pid != before[m]}))
        time.sleep(0.05)


APPLIED_BUT = "applied, but "  # set worked, the way back didn't (the app keys on this)

LOST_NOTE = ("\033[2J\033[H\n\n  ColorTune changed the screen's colours, but the desktop didn't get the screen\n"
             "  back{who}.\n\n  Press Ctrl+Alt+F{back} to go back to it. If the screen stays black, come back\n"
             "  here with Ctrl+Alt+F{spare} and try again, or restart the computer.\n")

SPARE_NOTE = ("\033[?25l\033[2J\033[H\n\n  ColorTune is changing the screen's colours. This takes a moment.\n\n"
              "  If this stays on screen, press Ctrl+Alt+F{vt}.\n")


def _switch(n, timeout=5.0):
    """Bring console n on screen; True once it's there. Leaving a desktop's console makes
    logind take the screen from the desktop first, and give it back on return."""
    try:
        _vt_ioctl("/dev/tty0", VT_ACTIVATE, n)
    except OSError:
        return False
    end = time.monotonic() + timeout
    while active_vt() != n:
        if time.monotonic() > end:
            return False
        time.sleep(0.02)
    return True


def display_manager():
    """The sign-in screen's systemd unit (e.g. greetd.service), or None."""
    try:
        p = subprocess.run(["systemctl", "show", "-p", "Id", "--value", "display-manager.service"],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    unit = p.stdout.strip()
    return unit if p.returncode == 0 and unit and unit != "display-manager.service" else None


def startup_dropins():
    return glob.glob(os.path.join(UNIT_DIR, "*.d", DROPIN))


def has_ctm():
    found = cards(read_only=True)
    for card in found:
        card.close()
    return bool(found)


def summary():
    """For the app, readable without root: {"now": saturation on the screen in use (None for
    a matrix from another program), "seen": whether a screen could be read at all,
    "startup": the value set at startup or None, "display_manager": its unit or None}."""
    now, found = 1.0, cards(read_only=True)
    for card in found:
        crtc = next((c for c in card.crtcs if c.active), None)
        if crtc is not None and now == 1.0:
            m = card.read(crtc.ctm_blob)
            now = saturation_of(m) if m else 1.0
        card.close()
    on = bool(startup_dropins()) and os.path.exists(LIBEXEC)
    return {"now": now, "seen": bool(found), "startup": read_conf() if on else None,
            "display_manager": display_manager()}


# ---------------------------------------------------------------- commands

def say(*args):
    print(*args, flush=True)


def fail(msg):
    print(f"colortune-ctm: {msg}", file=sys.stderr, flush=True)
    return 1


NOT_MASTER = ("The desktop is in charge of the screen right now, so only it can change the colour\n"
              "matrix. Switch to a text console (Ctrl+Alt+F3), sign in, and run this there.")


def _need_root():
    if os.geteuid() != 0:
        return fail("this needs administrator rights: run it with sudo")
    return None


def _master_cards(settle=0.0, hop=False):
    """Cards we are in charge of, or None and an explanation. settle: how long to keep
    trying while the desktop is still letting go of the screen. hop: we stepped aside from
    the desktop for this, so whoever has the screen isn't the desktop."""
    end = time.monotonic() + settle
    while True:
        found = cards()
        mine = [c for c in found if c.master()]
        for c in found:
            if c not in mine:
                c.close()
        if mine or time.monotonic() >= end:
            break
        time.sleep(0.1)
    if mine:
        return mine, None
    if not found:
        return None, fail("no screen with a colour matrix was found (or no permission to open it)")
    if hop:
        who = ", ".join(sorted({_comm(pid) for pid in (_master_tgids() or {}).values()}))
        return None, fail("another program took the screen while ColorTune stepped aside" +
                          (f" ({who})" if who else "") + "; nothing was changed")
    return None, fail(NOT_MASTER)


def _describe(card, crtc):
    m = card.read(crtc.ctm_blob)
    if m is None:
        what = "normal (no matrix)"
    else:
        s = saturation_of(m)
        what = f"saturation {s:.2f}" if s is not None else "a colour matrix from another program"
    return f"pipe {crtc.id} ({'in use' if crtc.active else 'idle'}): {what}"


def cmd_show(args):
    s = valid(args.saturation) if args.saturation else read_conf()
    if s is None:
        return fail(f"give a saturation between {MIN_S} and {MAX_S}, e.g. 1.25")
    driver = next((d for d in drivers() if d in QUANTISERS), None)
    rows = fit(s, driver)
    say(f"Saturation {s:.2f}" + (", rounded the way Intel hardware (Ice Lake and newer) holds it"
                                 if driver in QUANTISERS else ""))
    say("          R         G         B       sum")
    target = matrix(s)
    err = 0.0
    for name, row, t in zip("RGB", rows, target):
        vals = [c / ONE for c in row]
        err = max(err, *(abs(a - b) for a, b in zip(vals, t)))
        say(f"  {name}'  " + "  ".join(f"{v:+.5f}" for v in vals) + f"  {sum(row) / ONE:.6f}")
    say(f"Largest rounding error: {err:.5f}")
    return 0


def cmd_status(args):
    in_use = []
    with Console():  # on a text console, opening a card puts us in charge of the screen
        found = cards(read_only=True)
        if not found:
            say("No screen with a colour matrix could be opened.")
        for card in found:
            say(f"{card.path} ({card.driver})")
            for crtc in card.crtcs:
                say("  " + _describe(card, crtc))
                if crtc.active:
                    m = card.read(crtc.ctm_blob)
                    in_use.append(saturation_of(m) if m else 1.0)
            card.close()
    conf, dropins = read_conf(), startup_dropins()
    if os.path.exists(LIBEXEC) and not dropins:
        say("At startup: off (the helper is installed)")
    elif os.path.exists(LIBEXEC) and dropins:
        unit = os.path.basename(os.path.dirname(dropins[0]))[:-2]
        say(f"At startup: saturation {conf:.2f} before {unit} starts" if conf is not None else
            f"At startup: installed for {unit}, but {CONF} has no valid value")
        if skipped():
            say("This boot was started with colortune.ctm=0, so it was skipped.")
        elif conf is not None and any(v != conf for v in in_use):
            say("The screen isn't showing that value now. A change takes a restart; if it still\n"
                "doesn't show after one, something resets the colour matrix after startup.")
    else:
        say("At startup: not installed")
    return 0


def cmd_try(args):
    s = valid(args.saturation)
    if s is None:
        return fail(f"give a saturation between {MIN_S} and {MAX_S}, e.g. 1.25")
    err = _need_root()
    if err:
        return err
    with Console() as console:
        mine, err = _master_cards()
        if err:
            return err
        if console.text and console.fd < 0:
            for card in mine:
                card.close()
            return fail(NO_GUARD)
        before, vivid = {}, {}
        try:
            for card in mine:
                copies = {}
                for c in card.crtcs:
                    if c.ctm_blob not in copies:
                        copies[c.ctm_blob] = card.keep(c.ctm_blob)
                before[card] = {c.id: copies[c.ctm_blob] for c in card.crtcs}
                vivid[card] = card.create(pack(fit(s, card.driver)))
                card.lib.drmDropMaster(card.fd)  # in charge only while changing it
        except OSError as e:
            for card in mine:
                card.close()  # also frees the blobs this fd made
            return fail(f"nothing was changed: {e.strerror or e}")
        problem, shown = None, "as it was"
        try:
            # Everyday colours in palette slots 9-14: the console's own bright ones are pure
            # primaries, which a boost can't change. Loaded while nobody is in charge of the
            # screen, or the console can't apply them; ESC ] R puts the palette back.
            console.write(palette_codes())
            say("\nWatch these colours. They switch between normal and vivid every 2 seconds.\n")
            for code, block in ((41, " "), (91, "\u2588")):  # the console has no bright backgrounds
                for _ in range(3):
                    say("  " + "".join(f"\033[{code + i}m{block * 8}" for i in range(6)) + "\033[0m")
            say("  " + "".join(f"{name:<8}" for name in SAMPLES) + "\n")
            end, on = time.monotonic() + args.seconds, True
            while time.monotonic() < end and not problem and not console.leaving:
                for card in mine:
                    if card.lib.drmSetMaster(card.fd) and not card.master():
                        problem = "the desktop took the screen back"
                        break
                    problem = card.commit({c.id: vivid[card] if on else 0 for c in card.crtcs})
                    card.lib.drmDropMaster(card.fd)
                    if problem:
                        break
                    shown = f"vivid ({s:.2f})" if on else "normal"
                if not problem:
                    print(f"\r  Now: {'VIVID  ' + format(s, '.2f') if on else 'NORMAL'}      ", end="", flush=True)
                    console.wait(2)
                    on = not on
        except KeyboardInterrupt:
            pass
        finally:
            console.hold_interrupts()
            restored = True
            for card in mine:
                try:
                    if card.lib.drmSetMaster(card.fd) and not card.master():
                        restored = False
                    elif card.commit(before[card]):
                        restored = False
                finally:
                    card.close()
            console.write("\033]R")
        say("")
        if problem:
            fail(problem)
        if not restored:
            fail(f"could not put it back; the screen was left {shown}. To change it, run this\n"
                 "again from a text console with 'set off' (or 'set' and a value).")
        elif console.leaving:
            say("You switched away, so it stopped early. It's back to how it was.")
        else:
            say("Finished: back to how it was.")
    return 0 if restored and not problem else 1


def cmd_set(args):
    off = args.saturation == "off"
    s = 1.0 if off else valid(args.saturation)
    if s is None:
        return fail(f"give a saturation between {MIN_S} and {MAX_S} (e.g. 1.25), or off")
    err = _need_root()
    if err:
        return err
    from_desktop = active_vt() is not None and not on_text_console()
    status, problem = _set_from_desktop(s) if from_desktop else (_set_here(s), None)
    if status:
        return fail(problem) if problem else status
    startup = bool(startup_dropins())
    if startup:
        os.umask(0o022)
        _save(s)  # the value set at startup follows
    if off:
        say("Colour matrix removed" + (", at startup too." if startup else "."))
    else:
        say(f"Saturation {s:.2f} is on" + (", and set again at every startup." if startup else
                                            " until you restart or sign out."))
        if not from_desktop:
            say("Go back to the desktop to see it (usually Ctrl+Alt+F1). To remove it, run this with 'off'.")
    return fail(APPLIED_BUT + problem) if problem else status


def _set_here(s):
    """On a text console: set s, guarding the console meanwhile."""
    with Console() as console:
        console.hold_interrupts()  # it's quick, and must not stop half way
        if console.text and console.fd < 0:
            return fail(NO_GUARD)
        return _set_cards(s)


def _set_from_desktop(s):
    """The desktop is on screen and in charge of it. Step aside to a spare console, where it
    lets go of the screen, set s and come back, then wait until the desktop has it again.
    Returns (status of the set, None or what went wrong on the way back)."""
    back, spare = active_vt(), _spare_vt()
    if spare is None:
        return fail("there's no spare console to step aside to; nothing was changed"), None
    before, status = _master_tgids(), 1
    try:
        # Guarded before it comes on screen: from then on, a switch away waits until we
        # have let go of the screen, however fast someone presses Ctrl+Alt+F1.
        with Console(vt=spare) as guard:
            guard.hold_interrupts()
            if guard.fd < 0:
                status = fail("couldn't hold a spare console; nothing was changed")
            else:
                guard.write(SPARE_NOTE.format(vt=back))
                if not _switch(spare, timeout=30):
                    status = fail("the desktop didn't let go of the screen; nothing was changed")
                else:
                    status = _set_cards(s, settle=2.0, hop=True)
    finally:
        problem = _return_to(back, spare, before)
    return status, problem


def _return_to(back, spare, before):
    """Back to the desktop's console, until the desktop has the screen again. If someone else
    took it meanwhile, step aside and back once more so logind hands it over again.
    Returns None, or what to tell the person."""
    for attempt in range(2):
        if active_vt() != back and not (_switch(back) or _switch(back)):
            return f"couldn't switch back to the desktop: press Ctrl+Alt+F{back}"
        holder = _desktop_back(back, before)
        if holder is None:
            try:
                _vt_ioctl("/dev/tty0", VT_DISALLOCATE, spare)
            except OSError:
                pass
            return None
        if attempt == 0 and not _switch(spare):
            break
    # Never leave the person looking at a black screen: say what happened, on the spare.
    who = f" ({holder} has it)" if holder else ""
    try:
        _vt_write(spare, LOST_NOTE.format(who=who, back=back, spare=spare))
        _switch(spare)
    except OSError:
        pass
    return f"the desktop didn't get the screen back{who}. Press Ctrl+Alt+F{back} to go back to it"


def _set_cards(s, settle=0.0, hop=False):
    """Set s on every card we're in charge of (call it from a guarded console)."""
    mine, err = _master_cards(settle, hop)
    if err:
        return err
    status = 0
    for card in mine:
        try:
            problem = set_saturation(card, s)
        except OSError as e:
            problem = e.strerror or str(e)
        card.close()
        if problem:
            status = fail(f"{card.path}: {problem}")
    return status


def cmd_apply(args):
    """At startup, before the sign-in screen: quiet, quick, never blocks it for long."""
    if skipped():
        return 0
    s = read_conf()
    if s is None or s == 1.0:
        return 0
    deadline = time.monotonic() + 3
    done, status = set(), 0
    while True:
        pending = []
        for card in cards():
            if card.path not in done:
                if card.master():
                    try:
                        problem = set_saturation(card, s)
                    except OSError as e:
                        problem = e.strerror or str(e)
                    if problem:
                        status = fail(f"{card.path}: {problem}")
                    done.add(card.path)
                else:
                    pending.append(card.path)
            card.close()
        if (done and not pending) or time.monotonic() > deadline:
            break
        time.sleep(0.2)  # the graphics driver may still be starting, or the splash letting go
    for path in pending:
        status = fail(f"{path}: another program is in charge of the screen; skipped")
    if not done and not pending:
        status = fail("no screen with a colour matrix appeared; skipped")
    return status


def _write(path, text, mode=0o644):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".colortune.")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as f:
            f.write(text if isinstance(text, bytes) else text.encode())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _save(s):
    _write(CONF, "# Saturation ColorTune sets at startup (0.5 to 1.5, 1 = normal).\n"
                 "# Change it with: sudo colortune-ctm startup 1.2\n"
                 f"{s:.2f}\n")


def _install_helper():
    """The root-owned copy of this file, the command link and the polkit action."""
    if os.path.realpath(__file__) != LIBEXEC:
        with open(os.path.realpath(__file__), "rb") as f:
            _write(LIBEXEC, f.read(), 0o755)
    if not os.path.lexists(COMMAND):
        os.symlink(LIBEXEC, COMMAND)
    _write(POLICY, POLICY_XML)


def _startup_on(s):
    unit = display_manager()
    if unit is None:
        return fail("no display manager (sign-in screen service) was found")
    if not unit.startswith("greetd"):
        # GDM and LightDM take the screen over from the boot splash themselves, so there
        # is no moment to set it; X11 sign-in screens reset the matrix when they start.
        return fail(f"this only works with greetd (as on Ada); the sign-in screen here is {unit}.\n"
                    "Others take over from the boot splash themselves, or reset the colour matrix.")
    _install_helper()
    _save(s)
    timeout = shutil.which("timeout") or "/usr/bin/timeout"
    _write(os.path.join(UNIT_DIR, unit + ".d", DROPIN),
           "# ColorTune: set the screen's colour matrix (saturation) just before the sign-in\n"
           "# screen takes the display. Remove with: sudo colortune-ctm startup off\n"
           "[Service]\n"
           f"ExecStartPre=-{timeout} 8 {LIBEXEC} apply\n")
    subprocess.run(["systemctl", "daemon-reload"])
    say(f"Saturation {s:.2f} will be set at every startup, before {unit}.")
    return 0


def _remove(path, rmdir=False):
    try:
        os.remove(path)
    except FileNotFoundError:
        return
    if rmdir:
        try:
            os.rmdir(os.path.dirname(path))  # only if nothing else is in it
        except OSError:
            pass


def _startup_off():
    for path in startup_dropins():
        _remove(path, rmdir=True)
    _remove(CONF, rmdir=True)
    subprocess.run(["systemctl", "daemon-reload"])


def cmd_install(args):
    s = None
    if args.saturation is not None:
        s = valid(args.saturation)
        if s is None:
            return fail(f"give a saturation between {MIN_S} and {MAX_S}, e.g. 1.25")
    err = _need_root()
    if err:
        return err
    if s is not None and not (display_manager() or "").startswith("greetd"):
        return _startup_on(s)  # explains why, before anything is installed
    os.umask(0o022)  # files and directories others must read (status runs as the user)
    _install_helper()
    if s is not None:
        status = _startup_on(s)
        if status:
            return status
        say("Restart to see it, or apply it now with 'sudo colortune-ctm set %.2f'." % s)
    else:
        say(f"Installed {LIBEXEC} (also as colortune-ctm).")
    return 0


def cmd_startup(args):
    off = args.saturation == "off"
    s = None if off else valid(args.saturation)
    if not off and s is None:
        return fail(f"give a saturation between {MIN_S} and {MAX_S} (e.g. 1.25), or off")
    err = _need_root()
    if err:
        return err
    if off:
        _startup_off()
        say("It won't be set at startup any more. The screen stays as it is until you restart.")
        return 0
    os.umask(0o022)
    return _startup_on(s)


def cmd_uninstall(args):
    err = _need_root()
    if err:
        return err
    _startup_off()
    if os.path.realpath(COMMAND) == LIBEXEC:
        _remove(COMMAND)
    _remove(POLICY)
    _remove(LIBEXEC)
    say("Removed. The screen stays as it is until you restart.")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="colortune-ctm", description="Screen saturation through the "
                                 "graphics chip's colour matrix.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("show", help="the matrix for a saturation, as the hardware will apply it")
    p.add_argument("saturation", nargs="?")
    p.set_defaults(fn=cmd_show)
    sub.add_parser("status", help="what the screens and the startup setting are doing").set_defaults(fn=cmd_status)
    p = sub.add_parser("try", help="text console: switch between normal and vivid for a few seconds")
    p.add_argument("saturation")
    p.add_argument("--seconds", type=float, default=12)
    p.set_defaults(fn=cmd_try)
    for name, fn, text in (("set", cmd_set, "apply now ('off' removes it); from a desktop the screen "
                                             "blanks for a moment"),
                           ("startup", cmd_startup, "also set it at every startup ('off' stops)")):
        p = sub.add_parser(name, help=text)
        p.add_argument("saturation")
        p.set_defaults(fn=fn)
    p = sub.add_parser("install", help="install the helper; with a value, also set it at startup")
    p.add_argument("saturation", nargs="?")
    p.set_defaults(fn=cmd_install)
    sub.add_parser("uninstall", help="remove the helper and the startup step").set_defaults(fn=cmd_uninstall)
    sub.add_parser("apply", help="run at startup").set_defaults(fn=cmd_apply)
    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except BrokenPipeError:  # e.g. `status | head`
        sys.stdout = open(os.devnull, "w")
        return 1


if __name__ == "__main__":
    sys.exit(main())
