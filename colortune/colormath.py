"""Small, dependency-free colour maths: 3x3 matrices, xy/XYZ, CCT, gamut area."""

D65 = (0.3127, 0.3290)
D50_XYZ = (0.9642, 1.0, 0.8249)
SRGB = {"r": (0.64, 0.33), "g": (0.30, 0.60), "b": (0.15, 0.06), "w": D65}
DISPLAY_P3 = {"r": (0.680, 0.320), "g": (0.265, 0.690), "b": (0.150, 0.060), "w": D65}

# CIE 1931 2° spectral locus, xy, 380-700 nm
SPECTRAL_LOCUS = [
    (0.1741, 0.0050), (0.1738, 0.0049), (0.1733, 0.0048), (0.1726, 0.0048), (0.1714, 0.0051),
    (0.1689, 0.0069), (0.1644, 0.0109), (0.1566, 0.0177), (0.1440, 0.0297), (0.1355, 0.0399),
    (0.1241, 0.0578), (0.1096, 0.0868), (0.0913, 0.1327), (0.0687, 0.2007), (0.0454, 0.2950),
    (0.0235, 0.4127), (0.0082, 0.5384), (0.0039, 0.6548), (0.0139, 0.7502), (0.0389, 0.8120),
    (0.0743, 0.8338), (0.1142, 0.8262), (0.1547, 0.8059), (0.1929, 0.7816), (0.2296, 0.7543),
    (0.2658, 0.7243), (0.3016, 0.6923), (0.3373, 0.6589), (0.3731, 0.6245), (0.4087, 0.5896),
    (0.4441, 0.5547), (0.4788, 0.5202), (0.5125, 0.4866), (0.5448, 0.4544), (0.5752, 0.4242),
    (0.6029, 0.3965), (0.6270, 0.3725), (0.6482, 0.3514), (0.6658, 0.3340), (0.6801, 0.3197),
    (0.6915, 0.3083), (0.7006, 0.2993), (0.7079, 0.2920), (0.7140, 0.2859), (0.7190, 0.2809),
    (0.7230, 0.2770), (0.7260, 0.2740), (0.7300, 0.2700), (0.7334, 0.2666), (0.7347, 0.2653),
]
REC709_LUMA = (0.2126, 0.7152, 0.0722)

BRADFORD = ((0.8951, 0.2664, -0.1614),
            (-0.7502, 1.7135, 0.0367),
            (0.0389, -0.0685, 1.0296))


def mat_mul(a, b):
    return tuple(tuple(sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)) for i in range(3))


def mat_vec(m, v):
    return tuple(sum(m[i][k] * v[k] for k in range(3)) for i in range(3))


def mat_inv(m):
    (a, b, c), (d, e, f), (g, h, i) = m
    det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    if abs(det) < 1e-12:
        raise ValueError("singular matrix")
    return ((
        (e * i - f * h) / det, (c * h - b * i) / det, (b * f - c * e) / det), (
        (f * g - d * i) / det, (a * i - c * g) / det, (c * d - a * f) / det), (
        (d * h - e * g) / det, (b * g - a * h) / det, (a * e - b * d) / det))


def xy_to_XYZ(x, y, Y=1.0):
    if y == 0:
        return (0.0, 0.0, 0.0)
    return (x * Y / y, Y, (1 - x - y) * Y / y)


def XYZ_to_xy(X, Y, Z):
    s = X + Y + Z
    return (X / s, Y / s) if s else D65


def rgb_to_xyz_matrix(prim):
    """Matrix converting linear RGB to XYZ for primaries dict {r,g,b,w: (x,y)}."""
    cols = [xy_to_XYZ(*prim[c]) for c in "rgb"]
    m = tuple(tuple(cols[j][i] for j in range(3)) for i in range(3))
    s = mat_vec(mat_inv(m), xy_to_XYZ(*prim["w"]))
    return tuple(tuple(m[i][j] * s[j] for j in range(3)) for i in range(3))


def bradford(src_white_XYZ, dst_white_XYZ):
    s = mat_vec(BRADFORD, src_white_XYZ)
    d = mat_vec(BRADFORD, dst_white_XYZ)
    scale = ((d[0] / s[0], 0, 0), (0, d[1] / s[1], 0), (0, 0, d[2] / s[2]))
    return mat_mul(mat_inv(BRADFORD), mat_mul(scale, BRADFORD))


def cct(x, y):
    """Correlated colour temperature (McCamy's approximation)."""
    n = (x - 0.3320) / (0.1858 - y)
    return 449 * n ** 3 + 3525 * n ** 2 + 6823.3 * n + 5520.33


def _area(poly):
    return abs(sum(poly[i][0] * poly[i - 1][1] - poly[i - 1][0] * poly[i][1]
                   for i in range(len(poly)))) / 2


def _clip(subject, clipper):
    """Sutherland-Hodgman polygon clipping (clipper must be convex, CCW)."""
    def inside(p, a, b):
        return (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0]) >= 0

    def cross(p1, p2, a, b):
        dx, dy = p2[0] - p1[0], p2[1] - p1[1]
        ex, ey = b[0] - a[0], b[1] - a[1]
        t = (ex * (a[1] - p1[1]) - ey * (a[0] - p1[0])) / (ex * dy - ey * dx)
        return (p1[0] + t * dx, p1[1] + t * dy)

    out = list(subject)
    for i in range(len(clipper)):
        a, b = clipper[i - 1], clipper[i]
        inp, out = out, []
        for j in range(len(inp)):
            p, q = inp[j - 1], inp[j]
            if inside(q, a, b):
                if not inside(p, a, b):
                    out.append(cross(p, q, a, b))
                out.append(q)
            elif inside(p, a, b):
                out.append(cross(p, q, a, b))
        if not out:
            return []
    return out


def srgb_coverage(prim, reference=None):
    """(coverage %, relative area %) of a display's gamut versus sRGB (or reference) in xy."""
    reference = reference or SRGB

    def ccw(t):
        return t if _area_signed(t) > 0 else t[::-1]
    disp = ccw([prim["r"], prim["g"], prim["b"]])
    ref = ccw([reference["r"], reference["g"], reference["b"]])
    inter = _clip(ref, disp)
    a_ref = _area(ref)
    return (100 * _area(inter) / a_ref if inter else 0.0, 100 * _area(disp) / a_ref)


def _area_signed(poly):
    return sum(poly[i - 1][0] * poly[i][1] - poly[i][0] * poly[i - 1][1] for i in range(len(poly))) / 2


def xy_to_srgb(x, y):
    """Display colour for a chromaticity: brightest in-gamut sRGB, clipped, gamma encoded."""
    X, Y, Z = xy_to_XYZ(x, y)
    r = 3.2406 * X - 1.5372 * Y - 0.4986 * Z
    g = -0.9689 * X + 1.8758 * Y + 0.0415 * Z
    b = 0.0557 * X - 0.2040 * Y + 1.0570 * Z
    lo = min(r, g, b)
    if lo < 0:  # desaturate towards white until representable
        r, g, b = r - lo, g - lo, b - lo
    hi = max(r, g, b) or 1
    return tuple((v / hi) ** (1 / 2.2) for v in (r, g, b))


def saturation_matrix(s, w=REC709_LUMA):
    """3x3 RGB matrix scaling saturation around luma (s=1 identity)."""
    return tuple(tuple((1 - s) * w[j] + (s if i == j else 0) for j in range(3)) for i in range(3))
