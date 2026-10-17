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
        from colortune import config
        self.config = config
        self.old_state = config.STATE_FILE
        config.STATE_FILE = os.path.join(self.dir, "state.json")

    def tearDown(self):
        self.sf.ADA_INI = self.old
        self.config.STATE_FILE = self.old_state

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
        self.assertIsNone(self.sf.ada_set_saturation(0.5))
        self.assertEqual(self.sf.ada_filter_state(), (True, "protanopia", 100))

    def test_users_own_greyscale_is_not_ours(self):
        self.sf.ada_set({"ColorFilters": "on", "ColorFilter": "grayscale", "ColorFilterStrength": 100})
        self.assertEqual(self.sf.ada_saturation_status(), "theirs")
        self.assertIsNone(self.sf.ada_set_saturation(1.0))
        self.assertEqual(self.sf.ada_filter_state(), (True, "grayscale", 100))

    def test_users_filter_choice_restored(self):
        self.sf.ada_set({"ColorFilter": "deuteranopia", "ColorFilterStrength": 80})
        self.assertEqual(self.sf.ada_set_saturation(0.7), 0.7)
        self.assertEqual(self.sf.ada_filter_state(), (True, "grayscale", 30))
        self.sf.ada_set_saturation(1.0)
        self.assertEqual(self.sf.ada_filter_state(), (False, "deuteranopia", 80))
        self.assertEqual(self.sf.ada_saturation_status(), "free")

    def test_stale_record_never_claims_users_greyscale(self):
        self.sf.ada_set({"ColorFilter": "deuteranopia", "ColorFilterStrength": 80})
        self.sf.ada_set_saturation(0.6)
        self.sf.ada_set({"ColorFilters": "off"})          # user turns it off in Ada Settings
        self.assertEqual(self.sf.ada_saturation_status(), "free")
        self.assertEqual(self.sf.ada_filter_state(), (False, "deuteranopia", 80))
        self.sf.ada_set({"ColorFilters": "on", "ColorFilter": "grayscale", "ColorFilterStrength": 100})
        self.assertEqual(self.sf.ada_saturation_status(), "theirs")
        self.assertIsNone(self.sf.ada_set_saturation(1.0))
        self.assertEqual(self.sf.ada_filter_state(), (True, "grayscale", 100))

    def test_users_strength_change_makes_it_theirs(self):
        self.sf.ada_set_saturation(0.5)
        self.sf.ada_set({"ColorFilterStrength": 70})      # user drags Ada's own slider
        self.assertEqual(self.sf.ada_saturation_status(), "theirs")

    def test_snapping(self):
        self.assertEqual([self.sf.snap_saturation(v) for v in (0.94, 0.96, 0.43, -1)], [0.9, 1.0, 0.4, 0.0])

    def test_garbage_values_dont_crash(self):
        self.sf.ada_set({"ColorFilterStrength": "55.5x"})
        self.assertEqual(self.sf.ada_filter_state()[2], 100)
        with open(self.path, "ab") as f:
            f.write(b"Wallpaper=/home/\xe9t\xe9.png\n")
        self.sf.ada_set({"NightLight": "off"})
        self.assertIn(b"\xe9t\xe9", open(self.path, "rb").read())

    def test_header_at_end_of_file(self):
        with open(self.path, "w") as f:
            f.write("[General]\nX=1\n[Desktop]")
        self.sf.ada_set({"NightLight": "off"})
        self.assertEqual(self.sf.ada_get("NightLight"), "off")
        self.assertEqual(open(self.path).read(), "[General]\nX=1\n[Desktop]\nNightLight=off\n")


class AdaSections(AdaFilters):
    def test_key_only_in_other_section_is_not_touched(self):
        with open(self.path, "w") as f:
            f.write("[Desktop]\r\nTheme=midnight\r\n[Other]\r\nNightLight=keep\r\n")
        self.sf.ada_set({"NightLight": "off"})
        text = open(self.path, newline="").read()
        self.assertEqual(text, "[Desktop]\r\nNightLight=off\r\nTheme=midnight\r\n[Other]\r\nNightLight=keep\r\n")
        self.assertEqual(self.sf.ada_get("NightLight"), "off")
