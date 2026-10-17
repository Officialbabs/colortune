"""Software picture adjustments, turned into per-channel gamma ramps."""
from dataclasses import dataclass, asdict, fields

from .colormath import saturation_matrix


@dataclass
class Picture:
    gamma: float = 1.0       # relative; >1 lifts midtones, <1 deepens them
    black: float = 0.0       # +lifts shadows, -crushes near-black (deeper blacks)
    contrast: float = 1.0    # around mid-grey
    brightness: float = 1.0  # output scale (dims whites)
    red: float = 1.0         # white balance gains
    green: float = 1.0
    blue: float = 1.0
    saturation: float = 1.0  # only where a colour matrix backend exists

    @classmethod
    def from_dict(cls, d):
        names = {f.name for f in fields(cls)}
        return cls(**{k: float(v) for k, v in (d or {}).items() if k in names})

    def to_dict(self):
        return asdict(self)

    def lut_identity(self):
        return all(abs(getattr(self, k) - Picture.__dataclass_fields__[k].default) < 1e-6
                   for k in ("gamma", "black", "contrast", "brightness", "red", "green", "blue"))


def curve(p: Picture, gain: float, size: int = 256):
    out = []
    g = max(p.gamma, 0.05)
    for i in range(size):
        v = (i / (size - 1)) ** (1.0 / g)
        v = 0.5 + (v - 0.5) * p.contrast
        if p.black < 0:
            v = (v + p.black) / (1 + p.black)
        else:
            v = p.black + (1 - p.black) * v
        v *= gain * p.brightness
        out.append(min(1.0, max(0.0, v)))
    return out


def ramps(p: Picture, size: int = 256):
    return tuple(curve(p, gain, size) for gain in (p.red, p.green, p.blue))


def ramps_u16(p: Picture, size: int):
    return tuple([round(v * 65535) for v in ch] for ch in ramps(p, size))


def ctm(p: Picture):
    return saturation_matrix(p.saturation)
