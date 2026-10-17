"""Write ICC v2 display profiles (matrix/TRC + vcgt calibration curves)."""
import struct
import time

from .colormath import D50_XYZ, SRGB, bradford, rgb_to_xyz_matrix


def _s15(v):
    return struct.pack(">i", round(v * 65536))


def _xyz_tag(xyz):
    return b"XYZ " + b"\0" * 4 + b"".join(_s15(v) for v in xyz)


def _desc_tag(text):
    a = text.encode("ascii", "replace") + b"\0"
    return b"desc" + b"\0" * 4 + struct.pack(">I", len(a)) + a + b"\0" * 8 + b"\0" * 3 + b"\0" * 67


def _text_tag(text):
    return b"text" + b"\0" * 4 + text.encode("ascii", "replace") + b"\0"


def _curv_tag(gamma):
    return b"curv" + b"\0" * 4 + struct.pack(">IH", 1, round(gamma * 256)) + b"\0\0"


def _vcgt_tag(ramps):
    n = len(ramps[0])
    data = b"".join(struct.pack(">%dH" % n, *(round(min(1, max(0, v)) * 65535) for v in ch)) for ch in ramps)
    return b"vcgt" + b"\0" * 4 + struct.pack(">IHHH", 0, 3, n, 2) + data


def build_profile(description, primaries=None, gamma=2.2, ramps=None, gains=(1.0, 1.0, 1.0)):
    """A matrix/TRC profile describing the display *after* the vcgt curves are loaded.

    gamma is the calibration target the curves aim for; gains are the white-balance
    gains baked into the curves, which move the white point, so the colorants are
    scaled by them (in linear light) and the white is their sum.
    """
    prim = primaries or SRGB
    m = rgb_to_xyz_matrix(prim)
    lin = [max(g, 1e-3) ** gamma for g in gains]
    m = tuple(tuple(m[i][c] * lin[c] for c in range(3)) for i in range(3))
    white = tuple(sum(m[i]) for i in range(3))
    white = tuple(v / white[1] for v in white)
    m = tuple(tuple(v / sum(m[1]) for v in row) for row in m)
    adapt = bradford(white, D50_XYZ)
    cols = [tuple(sum(adapt[i][k] * m[k][c] for k in range(3)) for i in range(3)) for c in range(3)]

    tags = [
        (b"desc", _desc_tag(description)),
        (b"cprt", _text_tag("No copyright, use freely")),
        (b"wtpt", _xyz_tag(white)),
        (b"rXYZ", _xyz_tag(cols[0])),
        (b"gXYZ", _xyz_tag(cols[1])),
        (b"bXYZ", _xyz_tag(cols[2])),
    ]
    trc = _curv_tag(gamma)
    tags += [(b"rTRC", trc), (b"gTRC", trc), (b"bTRC", trc)]
    if ramps:
        tags.append((b"vcgt", _vcgt_tag(ramps)))

    table_size = 4 + 12 * len(tags)
    offset = 128 + table_size
    table, body, seen = [], b"", {}
    for sig, data in tags:
        if data in seen:  # shared TRC data
            table.append((sig, *seen[data]))
            continue
        pad = (-len(body)) % 4
        body += b"\0" * pad
        entry = (offset + len(body), len(data))
        seen[data] = entry
        table.append((sig, *entry))
        body += data
    body += b"\0" * ((-len(body)) % 4)
    size = 128 + table_size + len(body)

    t = time.gmtime()
    header = (struct.pack(">I", size) + b"\0" * 4 + struct.pack(">I", 0x02100000) + b"mntrRGB XYZ "
              + struct.pack(">6H", t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec)
              + b"acsp" + b"\0" * 4 + b"\0" * 4 + b"\0" * 4 + b"\0" * 4 + b"\0" * 8 + b"\0" * 4
              + b"".join(_s15(v) for v in D50_XYZ) + b"ctun" + b"\0" * 16 + b"\0" * 28)
    assert len(header) == 128
    tagtable = struct.pack(">I", len(tags)) + b"".join(sig + struct.pack(">II", o, s) for sig, o, s in table)
    return header + tagtable + body
