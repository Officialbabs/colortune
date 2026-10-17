"""Minimal EDID parser: identity, chromaticity, gamma."""
import hashlib


def parse(edid: bytes) -> dict:
    if len(edid) < 128 or edid[:8] != b"\x00\xff\xff\xff\xff\xff\xff\x00":
        return {}
    mfg = "".join(chr(((v) & 31) + 64) for v in (edid[8] >> 2, ((edid[8] & 3) << 3) | (edid[9] >> 5), edid[9]))
    info = {
        "vendor": mfg,
        "product": edid[10] | edid[11] << 8,
        "serial": int.from_bytes(edid[12:16], "little"),
        "year": edid[17] + 1990,
        "gamma": (edid[23] + 100) / 100 if edid[23] != 0xFF else 2.2,
        "md5": hashlib.md5(edid[:128]).hexdigest(),
        "name": "", "serial_text": "", "text": "",
    }
    for i in range(54, 126, 18):
        d = edid[i:i + 18]
        if d[0] == 0 and d[1] == 0:
            txt = d[5:].split(b"\n")[0].decode("ascii", "ignore").strip()
            if d[3] == 0xFC:
                info["name"] = txt
            elif d[3] == 0xFF:
                info["serial_text"] = txt
            elif d[3] == 0xFE and not info["text"]:
                info["text"] = txt

    lo1, lo2, h = edid[25], edid[26], edid[27:35]

    def c(hi, lo):
        return round(((hi << 2) | lo) / 1024, 4)
    prim = {
        "r": (c(h[0], lo1 >> 6), c(h[1], (lo1 >> 4) & 3)),
        "g": (c(h[2], (lo1 >> 2) & 3), c(h[3], lo1 & 3)),
        "b": (c(h[4], lo2 >> 6), c(h[5], (lo2 >> 4) & 3)),
        "w": (c(h[6], (lo2 >> 2) & 3), c(h[7], lo2 & 3)),
    }
    if all(prim[k][1] > 0 for k in prim):
        info["primaries"] = prim
    return info


def display_key(info: dict, connector: str) -> str:
    """Stable identifier that follows a monitor across ports."""
    if not info:
        return connector
    serial = info.get("serial_text") or str(info.get("serial") or "")
    return f"{info['vendor']}-{info['product']:04x}-{serial}" if serial and serial != "0" \
        else f"{info['vendor']}-{info['product']:04x}-{connector}"


def pretty_name(info: dict, connector: str) -> str:
    if not info:
        return connector
    model = info.get("name") or info.get("text") or f"{info['vendor']} {info['product']:04X}"
    if info.get("name") and info.get("text") and info["text"] not in model:
        model = f"{model} ({info['text']})"
    return f"{model} — {connector}"
