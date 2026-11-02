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


class ColourMatrix(unittest.TestCase):
    def test_intel_rounding(self):
        from colortune.kms import ONE, i915
        self.assertEqual(i915(ONE), ONE)
        self.assertEqual(i915(-ONE // 2), -ONE // 2)
        self.assertEqual(i915(ONE + 1000), ONE)                # 1/256 steps between 1 and 2
        self.assertEqual(i915(5 * ONE), i915(4 * ONE - 1))     # clamped below 4

    def test_rows_sum_to_one_and_fit_the_hardware(self):
        from colortune.kms import ONE, fit, i915, matrix
        for n in range(50, 151, 5):
            s = n / 100
            for driver in ("i915", None):
                rows = fit(s, driver)
                for row, target in zip(rows, matrix(s)):
                    self.assertEqual(sum(row), ONE, (s, driver))
                    for c, t in zip(row, target):
                        if driver:
                            self.assertEqual(i915(c), c)
                        self.assertLess(abs(c / ONE - t), 0.004)

    def test_pack_roundtrip_and_recognise(self):
        from colortune.kms import fit, pack, saturation_of, unpack
        data = pack(fit(1.3, "i915"))
        self.assertEqual(len(data), 72)
        self.assertEqual(struct.unpack("=9Q", data)[1] >> 63, 1)  # sign-magnitude, not two's complement
        self.assertEqual(saturation_of(unpack(data)), 1.3)
        self.assertIsNone(saturation_of([[1, 0, 0], [0, 0.5, 0.5], [0, 0, 1]]))

    def test_settings_file(self):
        import os
        import tempfile
        from colortune.kms import read_conf, skipped, valid
        self.assertEqual(valid("1.25"), 1.25)
        for bad in ("nan", "inf", "2", "0.1", "", "x", None):
            self.assertIsNone(valid(bad))
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "saturation")
            with open(path, "w") as f:
                f.write("# comment\n\n 1.2  # note\n")
            self.assertEqual(read_conf(path), 1.2)
            self.assertIsNone(read_conf(os.path.join(d, "missing")))
            with open(path, "w") as f:
                f.write("quiet splash colortune.ctm=0\n")
            self.assertTrue(skipped(path))
            with open(path, "w") as f:
                f.write("quiet colortune.ctm=01\n")
            self.assertFalse(skipped(path))

    def test_every_saturation_is_recognised_after_rounding(self):
        from colortune.kms import fit, pack, saturation_of, unpack
        for n in range(50, 151):
            for driver in ("i915", None):
                self.assertEqual(saturation_of(unpack(pack(fit(n / 100, driver)))), n / 100)

    def test_console_palette_escapes(self):
        from colortune.kms import palette_codes
        codes = palette_codes().split("\033]P")[1:]
        self.assertEqual([c[0] for c in codes], list("9ABCDE"))
        for c in codes:
            self.assertEqual(len(c), 7)
            int(c[1:], 16)

    def test_apply_waits_for_a_card_then_gives_up(self):
        from unittest import mock
        from colortune import kms

        class Clock:
            now = 0.0
            def monotonic(self):
                return self.now
            def sleep(self, t):
                self.now += t

        class FakeCard:
            def __init__(self, master):
                self.path, self._master, self.closed = "/dev/dri/card1", master, False
            def master(self):
                return self._master
            def close(self):
                self.closed = True

        applied = []
        with mock.patch.object(kms, "time", Clock()), \
                mock.patch.object(kms, "read_conf", return_value=1.25), \
                mock.patch.object(kms, "skipped", return_value=False), \
                mock.patch.object(kms, "set_saturation", lambda card, s: applied.append(s)), \
                mock.patch.object(kms, "fail", return_value=1) as fail:
            with mock.patch.object(kms, "cards", return_value=[]):
                self.assertEqual(kms.cmd_apply(None), 1)  # nothing appeared within 3 s
                self.assertIn("no screen", fail.call_args[0][0])
            busy = FakeCard(False)
            with mock.patch.object(kms, "cards", return_value=[busy]):
                self.assertEqual(kms.cmd_apply(None), 1)  # the splash never let go
                self.assertTrue(busy.closed)
            boots = iter([[], [], [FakeCard(True)]])
            with mock.patch.object(kms, "cards", lambda: next(boots, [])):
                self.assertEqual(kms.cmd_apply(None), 0)  # the driver came up late
            self.assertEqual(applied, [1.25])


class ColourBoost(unittest.TestCase):
    def test_policy_names_the_installed_helper(self):
        import xml.etree.ElementTree as ET
        from colortune.kms import LIBEXEC, POLICY_XML
        action = ET.fromstring(POLICY_XML.split("\n", 3)[3]).find("action")
        self.assertEqual(action.find("defaults/allow_active").text, "auth_admin_keep")
        self.assertEqual(action.find("annotate").text, LIBEXEC)

    def test_spare_console_is_one_nobody_has_open(self):
        from unittest import mock
        from colortune import kms
        busy = sum(1 << n for n in (1, 2, 3, 8, 9))

        def vt(busy, graphics=()):
            def ioctl(path, request, arg):
                if request == kms.VT_GETSTATE:
                    return struct.pack("HHH", 1, 0, busy)
                n = int(path[len("/dev/tty"):])
                if request == kms.KDGETMODE:
                    return struct.pack("i", 1 if n in graphics else kms.KD_TEXT)
                return struct.pack("bbhhh", kms.VT_AUTO, 0, 0, 0, 0)
            return ioctl
        with mock.patch.object(kms, "_vt_ioctl", vt(busy)):
            self.assertEqual(kms._spare_vt(), 10)
        with mock.patch.object(kms, "_vt_ioctl", vt(busy, graphics=(10, 11))):
            self.assertEqual(kms._spare_vt(), 12)  # a dead program's graphics console can't be left
        with mock.patch.object(kms, "_vt_ioctl", vt(0xffff)):
            self.assertIsNone(kms._spare_vt())

    def _set_from_desktop(self, set_cards=None, spare=8, switch_ok=True, back_ok=True, guard_ok=True,
                          desktop=(None,)):
        from unittest import mock
        from colortune import kms
        calls, vt, back = [], {"n": 1}, iter(desktop)

        def switch(n, timeout=5.0):
            calls.append(("switch", n))
            ok = back_ok if n == 1 else switch_ok
            if ok:
                vt["n"] = n
            return ok

        class Guard:
            def __init__(self, vt=None):
                self.vt, self.fd = vt, 5 if guard_ok else -1
            def __enter__(self):
                calls.append(("guard on", self.vt))
                return self
            def __exit__(self, *exc):
                calls.append(("guard off",))
            def hold_interrupts(self):
                pass
            def write(self, text):
                calls.append(("note", "Ctrl+Alt+F1" in text))

        def fail(msg):
            calls.append(("fail", msg))
            return 1
        with mock.patch.object(kms.os, "geteuid", return_value=0), \
                mock.patch.object(kms, "active_vt", lambda: vt["n"]), \
                mock.patch.object(kms, "on_text_console", lambda: vt["n"] != 1), \
                mock.patch.object(kms, "_spare_vt", return_value=spare), \
                mock.patch.object(kms, "_switch", switch), \
                mock.patch.object(kms, "Console", Guard), \
                mock.patch.object(kms, "_master_tgids", return_value={1: 1201}), \
                mock.patch.object(kms, "_desktop_back", lambda vt, before: calls.append(("wait", vt)) or next(back)), \
                mock.patch.object(kms, "_vt_ioctl", lambda *a: calls.append(("ioctl",) + a[1:])), \
                mock.patch.object(kms, "_set_cards", set_cards or (lambda s, settle, hop: calls.append(("set", s)) or 0)), \
                mock.patch.object(kms, "_vt_write", lambda n, text: calls.append(("lost note", n))), \
                mock.patch.object(kms, "startup_dropins", return_value=[]), \
                mock.patch.object(kms, "say"), mock.patch.object(kms, "fail", fail):
            try:
                rc = kms.main(["set", "1.25"])
            except RuntimeError:
                rc = "raised"
        return rc, calls

    def test_set_from_the_desktop_steps_aside_and_comes_back(self):
        from colortune.kms import VT_DISALLOCATE
        rc, calls = self._set_from_desktop()
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [("guard on", 8), ("note", True), ("switch", 8), ("set", 1.25), ("guard off",),
                                 ("switch", 1), ("wait", 1), ("ioctl", VT_DISALLOCATE, 8)])

    def test_set_always_comes_back_even_if_it_fails(self):
        def boom(*a, **k):
            raise RuntimeError
        rc, calls = self._set_from_desktop(set_cards=boom)
        self.assertEqual(rc, "raised")
        self.assertIn(("switch", 1), calls)

    def test_set_changes_nothing_without_a_spare_console_or_guard(self):
        rc, calls = self._set_from_desktop(spare=None)
        self.assertEqual(rc, 1)
        self.assertEqual([c[0] for c in calls], ["fail"])
        for kw in ({"switch_ok": False}, {"guard_ok": False}):
            rc, calls = self._set_from_desktop(**kw)
            self.assertEqual(rc, 1)
            self.assertNotIn("set", [c[0] for c in calls])
            self.assertIn("nothing was changed", [c for c in calls if c[0] == "fail"][0][1])

    def test_set_never_claims_success_when_it_cant_get_back(self):
        from colortune.kms import APPLIED_BUT
        rc, calls = self._set_from_desktop(back_ok=False)
        self.assertEqual(rc, 1)
        self.assertEqual([c for c in calls if c[0] == "switch"], [("switch", 8), ("switch", 1), ("switch", 1)])
        self.assertNotIn("wait", [c[0] for c in calls])
        self.assertTrue(calls[-1][1].startswith(APPLIED_BUT))  # it was set: the app must know
        self.assertIn("Ctrl+Alt+F1", calls[-1][1])

    def test_set_hands_the_screen_over_again_if_the_desktop_missed_it(self):
        rc, calls = self._set_from_desktop(desktop=("", None))
        self.assertEqual(rc, 0)
        self.assertEqual([c for c in calls if c[0] in ("switch", "wait")],
                         [("switch", 8), ("switch", 1), ("wait", 1), ("switch", 8), ("switch", 1), ("wait", 1)])
        rc, calls = self._set_from_desktop(desktop=("natter (5493)", "natter (5493)"))
        self.assertEqual(rc, 1)
        self.assertIn("natter (5493) has it", calls[-1][1])
        self.assertIn("Ctrl+Alt+F1", calls[-1][1])
        # never left on a black screen: the spare says what happened, and is shown
        self.assertEqual(calls[-3:-1], [("lost note", 8), ("switch", 8)])

    def test_desktop_back_waits_for_the_same_owner(self):
        from unittest import mock
        from colortune import kms
        clock = {"t": 0.0}

        def sleep(t):
            clock["t"] += t
        common = (mock.patch.object(kms.time, "sleep", sleep),
                  mock.patch.object(kms.time, "monotonic", lambda: clock["t"]),
                  mock.patch.object(kms, "_comm", lambda pid: f"intruder ({pid})"))
        seen = iter([{}, {1: 999}, {1: 1201}])
        with common[0], common[1], common[2], mock.patch.object(kms, "_session_active", return_value=False), \
                mock.patch.object(kms, "_master_tgids", lambda: next(seen)):
            self.assertIsNone(kms._desktop_back(1, {1: 1201}))
        # someone else has it, and logind has had its go (session active for 2 s): give up early
        clock["t"] = 0.0
        with common[0], common[1], common[2], mock.patch.object(kms, "_session_active", return_value=True), \
                mock.patch.object(kms, "_master_tgids", return_value={1: 999}):
            self.assertEqual(kms._desktop_back(1, {1: 1201}), "intruder (999)")
            self.assertLess(clock["t"], 3)
        # logind hasn't got to it yet (udev busy): keep waiting, up to the timeout
        clock["t"] = 0.0
        with common[0], common[1], common[2], mock.patch.object(kms, "_session_active", return_value=False), \
                mock.patch.object(kms, "_master_tgids", return_value={}):
            self.assertEqual(kms._desktop_back(1, {1: 1201}), "")
            self.assertGreater(clock["t"], 19)

    def test_master_parsing_follows_the_kernel_format(self):
        from unittest import mock
        from colortune import kms
        text = ("             command  tgid dev master a   uid      magic                 name                   id\n"
                "      systemd-logind  1201   1   y    y     0          0              <unset>                    5\n"
                "             natter2  5493   1   n    y  1000          0              <unset>                   12\n"
                "            Xwayland  9100 128   n    n  1000          0              <unset>                   13\n")
        with mock.patch.object(kms.glob, "glob", return_value=["clients"]), \
                mock.patch("builtins.open", mock.mock_open(read_data=text)):
            self.assertEqual(kms._master_tgids(), {1: 1201})
        with mock.patch.object(kms.glob, "glob", return_value=[]):
            self.assertIsNone(kms._master_tgids())

    def test_app_reads_the_helpers_answers(self):
        import subprocess
        from unittest import mock
        from colortune.backends import boost

        for code, err, want in ((0, "", None), (126, "", "cancelled"),
                                (127, "Error executing command as another user: No authentication agent found.",
                                 "no password dialog is available (Ada's polkit agent isn't running)"),
                                (127, "Error executing command as another user: Not authorized",
                                 "this account isn't allowed to make system changes"),
                                (1, "colortune-ctm: no screen with a colour matrix was found\n",
                                 "no screen with a colour matrix was found"),
                                (1, "Traceback (most recent call last):\n  ...\nOSError: disk full\n",
                                 "the helper stopped with an error: OSError: disk full")):
            done = subprocess.CompletedProcess([], code, "", err)
            with mock.patch.object(boost, "_helper", return_value=(True, False)), \
                    mock.patch.object(boost.subprocess, "run", return_value=done) as run:
                self.assertEqual(boost.run("set", "1.25"), want)
                self.assertEqual(run.call_args[0][0][:2], ["pkexec", boost.kms.LIBEXEC])
        with mock.patch.object(boost, "_helper", return_value=(True, False)), \
                mock.patch.object(boost.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            boost.run("install")  # set-up and updates come from this checkout
            self.assertEqual(run.call_args[0][0][2:4], ["-I", boost.os.path.realpath(boost.kms.__file__)])
