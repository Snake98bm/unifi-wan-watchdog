"""Settings tests: defaults, env seeding, validation, WAN order. Run: python tests/test_config.py"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import config  # noqa: E402


class ConfigTests(unittest.TestCase):
    def test_defaults_without_env(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            v = config.initial_values()
        self.assertEqual((v["down_minutes"], v["first_wan"], v["wan2_port"], v["wan1_port"]), (5, "wan2", 1, 8))
        self.assertEqual(config.wans_of(v), [("wan2", "AT&T", 1), ("wan1", "Verizon", 8)])
        self.assertTrue(v["dry_run"])
        self.assertIn("http://{ATT_GATEWAY}/cgi-bin/syslog.ha", v["capture_wan2"].split("\n"))
        with mock.patch.dict(os.environ, {}, clear=True):          # default AT&T address
            self.assertIn("http://192.168.1.254/cgi-bin/syslog.ha", [i["url"] for i in config.capture_urls(v)["wan2"]])

    def test_att_gateway_variable_fills_page_urls(self):
        with mock.patch.dict(os.environ, {"ATT_GATEWAY": "192.168.2.1", "UNIFI_API_KEY": "secret"}, clear=True):
            urls = config.capture_urls({"capture_wan2": "http://{ATT_GATEWAY}/cgi-bin/logs.ha",
                                        "capture_wan1": "http://{UNIFI_API_KEY}/x"})
        self.assertEqual([i["url"] for i in urls["wan2"]], ["http://192.168.2.1/cgi-bin/logs.ha"])
        self.assertEqual([i["url"] for i in urls["wan1"]], ["http://{UNIFI_API_KEY}/x"])   # secrets never filled in

    def test_at_lines_and_at_url(self):
        clean, errors = config.validate({"capture_wan1": 'at:AT+QENG="servingcell"\nat:AT+CSQ',
                                         "at_url_wan1": "https://{QUECTEL_GATEWAY}/cgi-bin/get_atcommand?atcmd={AT}"})
        self.assertEqual(errors, [])
        _, errors = config.validate({"capture_wan1": "at:rm -rf /", "at_url_wan1": "https://x/no-placeholder"})
        self.assertEqual(len(errors), 2)
        with mock.patch.dict(os.environ, {}, clear=True):
            items = config.capture_urls({**clean, "capture_wan2": ""})["wan1"]
        self.assertEqual(items[0]["url"],
                         "https://192.168.225.1/cgi-bin/get_atcommand?atcmd=AT%2BQENG%3D%22servingcell%22")
        self.assertEqual((items[0]["name"], items[0]["at"]), ("AT_QENG_servingcell", 'AT+QENG="servingcell"'))

    def test_gateway_with_scheme_or_slash_still_works(self):
        for value in ("https://192.168.225.1", "192.168.225.1/", "HTTP://192.168.225.1/", " 192.168.225.1 "):
            with mock.patch.dict(os.environ, {"QUECTEL_GATEWAY": value}, clear=True):
                self.assertEqual(config.expand("https://{QUECTEL_GATEWAY}/cgi-bin/x"), "https://192.168.225.1/cgi-bin/x")

    def test_only_gateway_placeholders_accepted(self):
        _, errors = config.validate({"capture_wan1": "http://{ATT_GATEWAY}/a.ha"})
        self.assertEqual(errors, [])
        _, errors = config.validate({"capture_wan1": "http://{UNIFI_API_KEY}/a.ha"})
        self.assertEqual(len(errors), 1)

    def test_old_env_vars_seed_first_run(self):
        env = {"DOWN_MINUTES": "7", "WANS": "wan1:Verizon:8,wan2:AT&T:1", "DRY_RUN": "false",
               "CAPTURE_WAN2": "http://192.168.1.254/a.ha,http://192.168.1.254/b.ha"}
        with mock.patch.dict(os.environ, env, clear=True):
            v = config.initial_values()
        self.assertEqual((v["down_minutes"], v["first_wan"], v["dry_run"]), (7, "wan1", False))
        self.assertEqual(v["capture_wan2"], "http://192.168.1.254/a.ha\nhttp://192.168.1.254/b.ha")

    def test_bad_env_value_falls_back_to_default(self):
        with mock.patch.dict(os.environ, {"DOWN_MINUTES": "lots"}, clear=True):
            self.assertEqual(config.initial_values()["down_minutes"], 5)

    def test_validate_ranges_and_types(self):
        clean, errors = config.validate({"down_minutes": "0", "cooldown_minutes": "abc", "private_minutes": "10"})
        self.assertEqual(clean, {"private_minutes": 10})
        self.assertEqual(len(errors), 2)

    def test_validate_urls_and_choices(self):
        clean, errors = config.validate({"capture_wan1": "http://a.b/x.ha\n\nhttp://a.b/y.ha",
                                         "capture_schedule": "daily", "first_wan": "wan1"})
        self.assertEqual(errors, [])
        self.assertEqual(clean["capture_wan1"], "http://a.b/x.ha\nhttp://a.b/y.ha")
        _, errors = config.validate({"capture_wan1": "javascript:alert(1)", "capture_schedule": "weekly",
                                     "first_wan": "wan9"})
        self.assertEqual(len(errors), 3)

    def test_full_form_unchecked_boxes_mean_off(self):
        clean, _ = config.validate({"down_minutes": "5"}, partial=False)
        self.assertEqual((clean["enabled"], clean["dry_run"]), (False, False))

    def test_monitor_apply_config_reorders_and_renames(self):
        for name in ("requests", "urllib3"):
            if name not in sys.modules:
                stub = type(sys)(name)
                stub.Session, stub.RequestException = object, Exception
                stub.exceptions = type("x", (), {"InsecureRequestWarning": Warning})
                stub.disable_warnings = lambda *a: None
                sys.modules[name] = stub
        from monitor import Monitor
        with mock.patch.dict(os.environ, {}, clear=True):
            v = config.initial_values()
        cfg = config.monitor_cfg(v)
        mon = Monitor(cfg, None, None, None)
        att = mon.wans[0]
        v.update(first_wan="wan1", wan2_label="AT&T DSL", wan2_port=2, down_minutes=9)
        mon.apply_config(config.monitor_cfg(v))
        self.assertEqual([w.key for w in mon.wans], ["wan1", "wan2"])
        self.assertIs(mon.wans[1], att)                     # same object: its timers/history are kept
        self.assertEqual((att.label, att.port, mon.cfg["down_minutes"]), ("AT&T DSL", 2, 9))


if __name__ == "__main__":
    unittest.main(verbosity=2)
