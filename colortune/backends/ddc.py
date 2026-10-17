"""Monitor hardware controls over DDC/CI using ddcutil."""
import re

from . import run, which

# MCCS VCP codes we expose: code -> (label, calibration steps, tip)
FEATURES = {
    0x10: ("Brightness", ("black",), "Backlight / black level on most LCDs, light output on OLED"),
    0x12: ("Contrast", ("white",), "White level - too high clips bright detail"),
    0x6C: ("Red black level", ("black",), ""),
    0x6E: ("Green black level", ("black",), ""),
    0x70: ("Blue black level", ("black",), ""),
    0x16: ("Red gain", ("white_balance",), "White balance"),
    0x18: ("Green gain", ("white_balance",), "White balance"),
    0x1A: ("Blue gain", ("white_balance",), "White balance"),
    0x8A: ("Saturation", ("saturation",), ""),
    0x87: ("Sharpness", (), "Too high adds halos"),
}
PRESET = 0x14          # colour preset (MCCS "Select Color Preset")
PRESETS = {0x01: "sRGB", 0x02: "Native", 0x05: "6500 K", 0x0B: "User"}


def available():
    return bool(which("ddcutil"))


def detect():
    """[{bus, connector, model}] for displays that answer DDC/CI."""
    if not available():
        return []
    code, out, _ = run(["ddcutil", "detect", "--terse"], timeout=30)
    found = []
    for block in out.split("\n\n"):
        if not block.startswith("Display"):
            continue  # "Invalid display" etc
        bus = re.search(r"/dev/i2c-(\d+)", block)
        conn = re.search(r"DRM[ _]connector:\s*(\S+)", block)
        model = re.search(r"Monitor:\s*(.+)", block)
        if bus:
            connector = conn.group(1).split("-", 1)[1] if conn and "-" in conn.group(1) else ""
            found.append({"bus": int(bus.group(1)), "connector": connector,
                          "model": model.group(1).strip() if model else ""})
    return found


def probe(bus):
    """{code: (current, maximum)} for supported continuous features."""
    codes = [f"{c:02X}" for c in FEATURES]
    res = {}
    rc, out, _ = run(["ddcutil", "--bus", str(bus), "--brief", "getvcp", *codes], timeout=30)
    if rc != 0 and not out.strip():  # ddcutil 1.x: one feature per call
        out = "\n".join(run(["ddcutil", "--bus", str(bus), "--brief", "getvcp", c], timeout=10)[1] for c in codes)
    for m in re.finditer(r"VCP ([0-9A-Fa-f]{2}) C (\d+) (\d+)", out):
        cur, mx = int(m.group(2)), int(m.group(3))
        if mx > 0:
            res[int(m.group(1), 16)] = (cur, mx)
    return res


def get_preset(bus):
    """Current colour preset code, or None if the monitor doesn't report one."""
    rc, out, _ = run(["ddcutil", "--bus", str(bus), "--brief", "getvcp", f"{PRESET:02X}"], timeout=10)
    m = re.search(r"VCP 14 SNC x([0-9A-Fa-f]+)", out)
    return int(m.group(1), 16) if m else None


def set_vcp(bus, code, value):
    run(["ddcutil", "--bus", str(bus), "--noverify", "setvcp", f"{code:02X}", str(int(round(value)))])


def set_preset(bus, code):
    """Gain controls usually only work in the monitor's 'User' colour mode - only
    ever changed when the user asks."""
    set_vcp(bus, PRESET, code)
