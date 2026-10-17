import struct
import unittest

from colortune.colormath import SRGB, srgb_coverage, xy_to_XYZ
from colortune.edid import parse
from colortune.icc import build_profile
from colortune.lut import Picture, ramps
from colortune import measure


class Core(unittest.TestCase):
    def test_identity_ramp(self):
        r, g, b = ramps(Picture(), 256)
        self.assertAlmostEqual(r[0], 0)
        self.assertAlmostEqual(r[128], 128 / 255)
        self.assertAlmostEqual(b[255], 1)

    def test_black_crush_and_lift(self):
        crushed = ramps(Picture(black=-0.05), 256)[0]
        lifted = ramps(Picture(black=0.05), 256)[0]
        self.assertEqual(crushed[5], 0)          # near-black pulled to black
        self.assertAlmostEqual(lifted[0], 0.05)
        self.assertAlmostEqual(crushed[255], 1)

    def test_gain(self):
        self.assertAlmostEqual(ramps(Picture(blue=0.9), 256)[2][255], 0.9)

    def test_coverage(self):
        self.assertAlmostEqual(srgb_coverage(SRGB)[0], 100, places=3)

    def test_icc_header_and_tags(self):
        data = build_profile("t", None, 2.2, ramps(Picture(gamma=1.1), 256))
        self.assertEqual(struct.unpack(">I", data[:4])[0], len(data))
        self.assertEqual(data[36:40], b"acsp")
        n = struct.unpack(">I", data[128:132])[0]
        sigs = {data[132 + 12 * i:136 + 12 * i] for i in range(n)}
        self.assertTrue({b"desc", b"wtpt", b"rXYZ", b"rTRC", b"vcgt"} <= sigs)

    def test_balance_cools_down_blue_cast(self):
        r, g, b = measure.balance(xy_to_XYZ(0.29, 0.30, 100), (1, 1, 1))
        self.assertEqual(r, 1.0)
        self.assertLess(b, 0.95)

    def test_edid(self):
        e = bytearray(128)
        e[:8] = b"\x00\xff\xff\xff\xff\xff\xff\x00"
        e[8:10] = bytes([0x10, 0xAC])   # DEL
        info = parse(bytes(e))
        self.assertEqual(info["vendor"], "DEL")


if __name__ == "__main__":
    unittest.main()


class AdaFilters(unittest.TestCase):
    def setUp(self):
        import os
        import tempfile
        from colortune.backends import screenfilters
        self.sf = screenfilters
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "desktop.ini")
        with open(self.path, "w") as f:
            f.write("[Desktop]\nNightLight=on\nColorFilters=off\nColorFilter=grayscale\n"
                    "ColorFilterStrength=100\nTheme=midnight\n\n[Other]\nColorFilters=keep\n")
        self.old = screenfilters.ADA_INI
        screenfilters.ADA_INI = self.path

    def tearDown(self):
        self.sf.ADA_INI = self.old

    def test_saturation_roundtrip_keeps_other_lines(self):
        self.sf.ada_set_saturation(0.6)
        self.assertEqual(self.sf.ada_filter_state(), (True, "grayscale", 40))
        self.assertAlmostEqual(self.sf.ada_get_saturation(), 0.6)
        self.sf.ada_set_saturation(1.0)
        text = open(self.path).read()
        self.assertIn("ColorFilters=off\n", text)
        self.assertIn("Theme=midnight", text)
        self.assertIn("[Other]\nColorFilters=keep", text)

    def test_other_filter_is_respected(self):
        self.sf.ada_set({"ColorFilters": "on", "ColorFilter": "protanopia"})
        self.assertFalse(self.sf.ada_saturation_usable())


class AdaSections(AdaFilters):
    def test_key_only_in_other_section_is_not_touched(self):
        with open(self.path, "w") as f:
            f.write("[Desktop]\r\nTheme=midnight\r\n[Other]\r\nNightLight=keep\r\n")
        self.sf.ada_set({"NightLight": "off"})
        text = open(self.path, newline="").read()
        self.assertEqual(text, "[Desktop]\r\nNightLight=off\r\nTheme=midnight\r\n[Other]\r\nNightLight=keep\r\n")
        self.assertEqual(self.sf.ada_get("NightLight"), "off")
