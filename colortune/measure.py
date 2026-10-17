"""Colorimeter / spectrometer readings through ArgyllCMS `spotread`."""
import os
import re
import shlex

from .backends import run, which
from .colormath import D65, SRGB, XYZ_to_xy, cct, mat_inv, mat_vec, rgb_to_xyz_matrix, xy_to_XYZ


def available():
    return bool(which("spotread"))


def read():
    """One reading -> (X, Y, Z) with Y in cd/m². Raises RuntimeError."""
    extra = shlex.split(os.environ.get("COLORTUNE_SPOTREAD_ARGS", ""))
    rc, out, err = run(["spotread", "-x", "-O", *extra], timeout=120)
    m = re.search(r"XYZ:\s*(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)", out)
    if not m:
        raise RuntimeError((err or out).strip().splitlines()[-1] if (err or out).strip() else "no reading")
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
