# ColorTune

Calibrate blacks, gamma, white balance and saturation on any Linux desktop.

ColorTune detects what your system allows and uses the best method available for each screen:

| Layer | What it adjusts | How | Where it works |
|---|---|---|---|
| **Display hardware** (preferred) | Backlight, brightness, contrast, RGB gain, black level, saturation | `/sys/class/backlight` via logind; DDC/CI via `ddcutil` | Laptop panels; most external monitors (X11 and Wayland) |
| **Picture curves** | Black point, gamma, contrast, white level, RGB gains | XRandR gamma ramps | Any X11 desktop |
| | | `wlr-gamma-control` (small daemon keeps it applied) | Sway, Hyprland, river, labwc, Wayfire, niri… |
| | | ICC profile with `vcgt` curves via colord / `kscreen-doctor` | GNOME, KDE Plasma 6 |
| **Saturation** | Colour matrix | NVIDIA Digital Vibrance, X11 `CTM` property (AMD/Intel), or monitor DDC | See left |
| | Reduce only | Ada's colour filter (greyscale intensity) | Ada desktop |
| **Night Light check** | Warns when the screen is being warmed, and can pause it | Ada, GNOME (until tomorrow); KDE, gammastep, redshift, wlsunset (warning only) | |
| **Measurement** | Luminance, contrast ratio, white point, auto white balance to D65 | ArgyllCMS `spotread` + any supported colorimeter | Everywhere |

Hardware controls come first because they change the display itself. Curves applied in the graphics pipeline cut into the available levels, so big moves can cause banding.

## Install

```bash
./install.sh
```

The installer adds the dependencies (PyGObject with GTK 4 and libadwaita 1.4+, pycairo, ddcutil, colord, ArgyllCMS), loads `i2c-dev` for DDC/CI, and adds `colortune` to `~/.local/bin` and your app menu. Everything except PyGObject + GTK 4 is optional.

You can also run it without installing:

```bash
bin/colortune
```

## Use

The app has five pages: **Overview** (your display's colour range plotted against sRGB and Display P3, and what can be adjusted), **Adjust**, **Check** (test patterns), **Measure** (colorimeter) and **Profiles**. The interface is neutral grey on purpose, because coloured interfaces change how you judge white.

1. If Night Light is on, pause it from the banner. It warms the screen and throws off every judgement.
2. Run **Guided calibration**. You'll step through full-screen patterns: black level → white level → gamma → white balance → saturation → uniformity. Each step shows only the sliders that matter for it. Use **H** to hide the panel and **Esc** to close.
3. Save the result as a profile (for example *Movies*, *Night* or *Photo editing*).
4. Turn on **Apply at login** on the Profiles page.

If you have a colorimeter, the **Measure** page measures white and black luminance, contrast ratio and colour temperature. **Auto white balance** adjusts the RGB gains in a loop until white hits D65.

### Command line

```bash
colortune detect                          # what each display supports
colortune set black=-0.02 gamma=1.05      # picture curves
colortune set -d DP-1 vcp10=30 vcp12=70   # monitor brightness/contrast over DDC/CI
colortune profile save Movies
colortune apply -p Movies
colortune reset
```

## Honest limits

- **Gamut can't be added.** A panel that covers about 60% of sRGB, as many budget laptop panels do, can't show sRGB colours at full saturation. Raising Saturation makes colours punchier but less accurate.
- **Blacks on LCD:** the deepest black comes from lowering the backlight or monitor brightness. A negative *Black point* hides the grey glow on near-black content, but it also hides shadow detail, so keep it small.
- **Wayland compositors that offer no colour API** (only some do) limit ColorTune to the hardware controls and test patterns. `colortune detect` shows this.
- **Saturation can only be raised** where something can apply a colour matrix: X11 with a CTM-capable driver, NVIDIA, or a monitor with a DDC saturation control. On Ada, saturation can only be lowered, through its colour filter.
- Without a colorimeter, calibration is judged by eye. It can still get close, but only a measurement is objective.

## Files

- `~/.config/colortune/state.json`: current settings per display. Keys follow the monitor's EDID, so settings move with the monitor across ports.
- `~/.config/colortune/profiles.json`: saved profiles.
- `~/.local/share/colortune/profiles/*.icc`: generated ICC profiles (on GNOME and KDE).

## Tests

```bash
python3 -m unittest tests/test_core.py
```

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
