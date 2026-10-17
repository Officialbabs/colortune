"""Colorimeter / spectrometer readings through ArgyllCMS `spotread`."""
import os
import re
import shlex
import subprocess
import time

from .backends import run, which
from .colormath import D65, SRGB, XYZ_to_xy, cct, mat_inv, mat_vec, rgb_to_xyz_matrix, xy_to_XYZ


class Cancelled(Exception):
    pass


def available():
    """ArgyllCMS is installed (a meter may or may not be plugged in - see probe())."""
    return bool(which("spotread"))


def probe():
    """{"instruments": [names], "types": [(letter, description)]} from spotread's usage text.

    The display-type list (LCD with LED or CCFL backlight, OLED, refresh...) only appears
    when a meter is connected, and is specific to it.
    """
    if not available():
        return {"instruments": [], "types": []}
    rc, out, err = run(["spotread", "-?"], timeout=15)
    text = out + err
    instruments, types, section = [], [], None
    for line in text.splitlines():
        if re.match(r"^ -c\s", line):
            section = "c"
            continue
        if re.match(r"^ -y\s", line):
            section = "y"
            continue
        if re.match(r"^ -\S", line):
            section = None
            continue
        if section == "c":
            m = re.match(r"^\s+\d+\s*=\s*'(.*)'", line)
            if m:
                name = re.search(r"\((.*)\)\s*$", m.group(1))
                instruments.append(name.group(1) if name else m.group(1))
        elif section == "y":
            m = re.match(r"^\s{3,}(\w)\s+(.+?)\s*$", line)
            if m:
                types.append((m.group(1), m.group(2)))
    return {"instruments": instruments, "types": types}


def read(display_type=None, cancel=None, timeout=120):
    """One reading -> (X, Y, Z) with Y in cd/m². Raises RuntimeError or Cancelled.

    cancel: a threading.Event; setting it stops spotread at once.
    """
    args = ["spotread", "-x", "-O"]
    if display_type:
        args += ["-y", display_type]
    args += shlex.split(os.environ.get("COLORTUNE_SPOTREAD_ARGS", ""))
    p = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        p.stdin.write("\n")  # some versions wait for a key before reading
        p.stdin.flush()
    except OSError:
        pass
    end = time.time() + timeout
    while p.poll() is None:
        if cancel is not None and cancel.is_set():
            p.terminate()
            p.wait(timeout=5)
            raise Cancelled()
        if time.time() > end:
            p.kill()
            raise RuntimeError("the meter didn't respond in time")
        time.sleep(0.1)
    out = p.stdout.read()
    m = re.search(r"XYZ:\s*(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)", out)
    if not m:
        last = [ln for ln in out.strip().splitlines() if ln.strip()]
        msg = last[-1] if last else "no reading"
        if "No instrument" in out or "No ports" in out:
            msg = "no colorimeter found. Plug it in and try again"
        raise RuntimeError(msg)
    return tuple(float(v) for v in m.groups())


def describe(xyz):
    X, Y, Z = xyz
    x, y = XYZ_to_xy(X, Y, Z)
    return {"Y": Y, "x": x, "y": y, "cct": cct(x, y) if Y > 0.05 else None,
            "dxy": ((x - D65[0]) ** 2 + (y - D65[1]) ** 2) ** 0.5}


def balance(xyz, gains, linear=False, primaries=None):
    """New (r, g, b) gains that move a white reading towards D65.

    linear=True for monitor (DDC) gains, which act on light output; False for
    gamma-ramp gains, which act on the encoded signal (~gamma 2.2).
    """
    m_inv = mat_inv(rgb_to_xyz_matrix(primaries or SRGB))
    X, Y, Z = xyz
    meas = mat_vec(m_inv, (X / Y, 1.0, Z / Y))
    target = mat_vec(m_inv, xy_to_XYZ(*D65))
    ratio = [max(t / m, 0.01) if m > 0 else 1.0 for t, m in zip(target, meas)]
    if not linear:
        ratio = [r ** (1 / 2.2) for r in ratio]
    new = [g * r for g, r in zip(gains, ratio)]
    top = max(new)
    return tuple(min(1.0, n / top) for n in new)
