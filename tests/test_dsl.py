"""Bonded-DSL checks (rates since the reboot, the post-reboot minimum, error counts). Run: python tests/test_dsl.py"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import dsl  # noqa: E402

H = 3600
BOOT = 1_000_000


def line(name, state="Up", down=30000, up=6500, ds_max=67000, us_max=12400, fec=(0, 0), crc=(0, 0)):
    return {"line": name, "state": state, "down": down, "up": up, "ds_max": ds_max, "us_max": us_max,
            "fec": list(fec), "crc": list(crc)}


def sample(hours, l1=None, l2=None, boot=BOOT):
    """A save `hours` after the boot (box uptime known unless boot is None)."""
    return {"ts": BOOT + hours * H, "boot": boot, "lines": [l1 or line("Line 1"), l2 or line("Line 2")]}


class RateTests(unittest.TestCase):
    def test_steady_line_is_quiet(self):
        self.assertEqual(dsl.checks([sample(0.1), sample(5), sample(10)], "AT&T"), {})

    def test_total_sync_drop_since_the_reboot(self):
        s = [sample(0.1), sample(5, l2=line("Line 2", down=20000))]      # 60 -> 50 Mbps = -17%
        out = dsl.checks(s, "AT&T")
        self.assertEqual(list(out), ["sync_down"])
        self.assertIn("downstream sync down 17% since the reboot: 50.0 Mbps now, 60.0 Mbps after", out["sync_down"])

    def test_one_pair_carrying_the_bond_is_not_a_sync_drop(self):
        # the box re-profiles: Line 1 drops, Line 2 alone syncs at what both did
        s = [sample(0.1), sample(5, l1=line("Line 1", "Down", 0, 0, 0, 0), l2=line("Line 2", down=60000, up=13000))]
        self.assertNotIn("sync_down", dsl.checks(s, "AT&T"))

    def test_attainable_drop_per_line(self):
        s = [sample(0.1), sample(5, l1=line("Line 1", ds_max=58000))]    # 67 -> 58 = -13%
        out = dsl.checks(s, "AT&T", min_down=0)
        self.assertEqual(list(out), ["att_Line 1_ds_max"])
        self.assertIn("Line 1 max attainable downstream down 13%", out["att_Line 1_ds_max"])
        self.assertEqual(dsl.checks(s, "AT&T", drop_pct=15, min_down=0), {})

    def test_a_new_boot_is_a_new_baseline(self):
        s = [sample(0.1), sample(5, l1=line("Line 1", ds_max=50000), boot=BOOT + 4 * H)]
        self.assertNotIn("att_Line 1_ds_max", dsl.checks(s, "AT&T", min_down=0))

    def test_minimum_after_the_reboot(self):
        s = [sample(0.1, l2=line("Line 2", ds_max=59000)), sample(5, l2=line("Line 2", ds_max=59000))]
        out = dsl.checks(s, "AT&T")
        self.assertEqual(list(out), ["floor_Line 2"])
        self.assertIn("only 59.0/12.4 Mbps (want at least 60.0/10.0)", out["floor_Line 2"])
        s = [sample(0.1, l1=line("Line 1", us_max=9500))]                # upstream too, and on the first save
        self.assertEqual(list(dsl.checks(s, "AT&T")), ["floor_Line 1"])
        self.assertEqual(dsl.checks(s, "AT&T", min_up=0), {})

    def test_no_uptime_no_rate_checks(self):
        s = [sample(0.1, boot=None), sample(5, l1=line("Line 1", ds_max=10000), boot=None)]
        self.assertEqual(dsl.checks(s, "AT&T"), {})


class ErrorTests(unittest.TestCase):
    def week(self, fec_per_hour=800, crc_per_hour=0.5, hours=48):
        """Hourly saves with steady error counts (cumulative since the line trained)."""
        return [sample(h, l2=line("Line 2", fec=(fec_per_hour * h, 0), crc=(int(crc_per_hour * h), 0)))
                for h in range(1, hours + 1)]

    def add(self, s, fec, crc):
        last = s[-1]["lines"][1]
        h = (s[-1]["ts"] - BOOT) / H + 1
        return s + [sample(h, l2=line("Line 2", fec=(last["fec"][0] + fec, 0), crc=(last["crc"][0] + crc, 0)))]

    def test_normal_hour_is_quiet(self):
        self.assertEqual(dsl.checks(self.add(self.week(), 900, 1), "AT&T"), {})

    def test_crc_above_normal(self):
        out = dsl.checks(self.add(self.week(), 900, 40), "AT&T")
        self.assertEqual(list(out), ["crc_Line 2"])
        self.assertIn("CRC errors high: 40/hour in the last hour, 1/hour on average", out["crc_Line 2"])

    def test_fec_needs_factor_and_floor(self):
        self.assertIn("fec_Line 2", dsl.checks(self.add(self.week(), 5000, 0), "AT&T"))
        self.assertEqual(dsl.checks(self.add(self.week(), 2000, 0), "AT&T"), {})          # < 3x 800
        self.assertEqual(dsl.checks(self.add(self.week(fec_per_hour=10), 900, 0), "AT&T"), {})   # < floor 1000

    def test_needs_a_day_of_history(self):
        self.assertEqual(dsl.checks(self.add(self.week(hours=12), 0, 500), "AT&T"), {})

    def test_retrain_reset_is_not_counted(self):
        s = self.week()
        s.append(sample(49, l2=line("Line 2", fec=(10, 0), crc=(0, 0))))     # counters went back to ~0
        self.assertEqual(dsl.checks(s, "AT&T"), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
