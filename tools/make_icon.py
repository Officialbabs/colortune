"""Generate ColorTune's app icon (GNOME-style 128px canvas), symbolic icon and README image.

The icon is the app's own idea: a neutral grey tile, colour only in the content - the
CIE 1931 chromaticity diagram with a display's gamut triangle drawn on it.

    python3 tools/make_icon.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
APPS = os.path.join(ROOT, "data", "icons", "hicolor", "scalable", "apps")
SYMBOLIC = os.path.join(ROOT, "data", "icons", "hicolor", "symbolic", "apps")
from colortune.colormath import SPECTRAL_LOCUS, SRGB

# chart area inside the tile
X0, Y0, W, H = 26, 18, 78, 84          # px
XR, YR = (0.0, 0.75), (0.0, 0.85)


def pt(x, y):
    return (X0 + (x - XR[0]) / (XR[1] - XR[0]) * W, Y0 + (YR[1] - y) / (YR[1] - YR[0]) * H)


locus = " ".join(f"{'M' if i == 0 else 'L'}{px:.2f},{py:.2f}" for i, (px, py) in
                 enumerate(pt(x, y) for x, y in SPECTRAL_LOCUS)) + " Z"
# a narrow-ish display gamut, like the panel this was built on
tri = [pt(*SRGB["r"]), pt(*SRGB["g"]), pt(*SRGB["b"])]
tri_d = "M" + " L".join(f"{x:.2f},{y:.2f}" for x, y in tri) + " Z"
wx, wy = pt(0.3127, 0.3290)
gx, gy = pt(0.17, 0.80)    # green tip
rx, ry = pt(0.73, 0.27)    # red end
bx, by = pt(0.17, 0.0)     # blue corner

svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128" viewBox="0 0 128 128">
  <defs>
    <linearGradient id="tile" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#3a3a3a"/>
      <stop offset="1" stop-color="#232323"/>
    </linearGradient>
    <linearGradient id="gb" gradientUnits="userSpaceOnUse" x1="{gx:.1f}" y1="{gy:.1f}" x2="{bx:.1f}" y2="{by:.1f}">
      <stop offset="0" stop-color="#2ee06a"/>
      <stop offset="0.55" stop-color="#18b7c9"/>
      <stop offset="1" stop-color="#3a3dff"/>
    </linearGradient>
    <linearGradient id="red" gradientUnits="userSpaceOnUse" x1="{X0 + 10:.1f}" y1="0" x2="{rx:.1f}" y2="0">
      <stop offset="0.35" stop-color="#ff3b30" stop-opacity="0"/>
      <stop offset="0.75" stop-color="#ffb21e" stop-opacity="0.85"/>
      <stop offset="1" stop-color="#ff3b30"/>
    </linearGradient>
    <linearGradient id="mag" gradientUnits="userSpaceOnUse" x1="0" y1="{wy:.1f}" x2="0" y2="{by:.1f}">
      <stop offset="0" stop-color="#d63bd6" stop-opacity="0"/>
      <stop offset="1" stop-color="#c02fd0" stop-opacity="0.9"/>
    </linearGradient>
    <radialGradient id="white" gradientUnits="userSpaceOnUse" cx="{wx:.1f}" cy="{wy:.1f}" r="22">
      <stop offset="0" stop-color="#ffffff" stop-opacity="0.9"/>
      <stop offset="1" stop-color="#ffffff" stop-opacity="0"/>
    </radialGradient>
    <clipPath id="locus"><path d="{locus}"/></clipPath>
  </defs>
  <!-- tile with a bottom lip, GNOME style -->
  <rect x="10" y="14" width="108" height="104" rx="22" fill="#141414"/>
  <rect x="10" y="10" width="108" height="104" rx="22" fill="url(#tile)"/>
  <rect x="10.5" y="10.5" width="107" height="103" rx="21.5" fill="none" stroke="#ffffff" stroke-opacity="0.08"/>
  <!-- chromaticity diagram -->
  <g clip-path="url(#locus)">
    <rect x="0" y="0" width="128" height="128" fill="url(#gb)"/>
    <rect x="0" y="0" width="128" height="128" fill="url(#mag)"/>
    <rect x="0" y="0" width="128" height="128" fill="url(#red)"/>
    <rect x="0" y="0" width="128" height="128" fill="url(#white)"/>
  </g>
  <path d="{locus}" fill="none" stroke="#000000" stroke-opacity="0.25" stroke-width="1"/>
  <!-- display gamut -->
  <path d="{tri_d}" fill="#ffffff" fill-opacity="0.12" stroke="#101010" stroke-opacity="0.55" stroke-width="6.5" stroke-linejoin="round"/>
  <path d="{tri_d}" fill="none" stroke="#ffffff" stroke-width="3.6" stroke-linejoin="round"/>
''' + "".join(f'''  <circle cx="{x:.2f}" cy="{y:.2f}" r="5" fill="#101010" fill-opacity="0.55"/>
  <circle cx="{x:.2f}" cy="{y:.2f}" r="3.4" fill="#ffffff"/>
''' for x, y in tri) + '''</svg>
'''
with open(os.path.join(APPS, "ai.stuard.ColorTune.svg"), "w") as f:
    f.write(svg)

# symbolic: 16px, single colour, outline of the diagram + filled triangle
def spt(x, y):
    return (1.5 + x / 0.75 * 13, 1 + (0.85 - y) / 0.85 * 14)
sl = " ".join(f"{'M' if i == 0 else 'L'}{px:.2f},{py:.2f}" for i, (px, py) in
              enumerate(spt(x, y) for x, y in SPECTRAL_LOCUS[::2] + [SPECTRAL_LOCUS[-1]])) + " Z"
st = "M" + " L".join(f"{x:.2f},{y:.2f}" for x, y in (spt(*SRGB[c]) for c in "rgb")) + " Z"
sym = f'''<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">
  <path d="{sl}" fill="none" stroke="#222222" stroke-width="1.3" stroke-linejoin="round"/>
  <path d="{st}" fill="#222222"/>
</svg>
'''
with open(os.path.join(SYMBOLIC, "ai.stuard.ColorTune-symbolic.svg"), "w") as f:
    f.write(sym)

# README image
import gi  # noqa: E402

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf  # noqa: E402

pb = GdkPixbuf.Pixbuf.new_from_file_at_size(os.path.join(APPS, "ai.stuard.ColorTune.svg"), 256, 256)
pb.savev(os.path.join(ROOT, "docs", "icon.png"), "png", [], [])
print("icons written")
