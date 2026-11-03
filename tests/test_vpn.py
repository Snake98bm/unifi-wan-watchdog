"""VPN probe: Cloudflare ban detection and the watchdog's reconnect logic. Run: python tests/test_vpn.py"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from test_monitor import CFG, Clock, FakeBouncer, FakeGateway  # noqa: E402  (also stubs requests/urllib3)

import config  # noqa: E402
import probe  # noqa: E402
from monitor import Monitor  # noqa: E402
from store import Events, Probes, Settings  # noqa: E402

# trimmed versions of what Cloudflare sends for error 1006 (browser and plain) and for a challenge
BAN_HTML = ('<title>Access denied | site.example used Cloudflare to restrict access</title>'
            '<span class="cf-error-code">1006</span><p>The owner of this website (site.example) has banned your IP '
            'address (198.51.100.77).</p>')
BAN_PLAIN = "error code: 1006"
CHALLENGE = '<title>Just a moment...</title><script src="/cdn-cgi/challenge-platform/h/b/orchestrate"></script>'
VPN_EXIT = "192.0.2.77"


def dump(obj, path):
    with open(path, "w") as f:
        json.dump(obj, f)


def load(path):
    with open(path) as f:
        return json.load(f)


class BanParseTests(unittest.TestCase):
    def test_ban_pages(self):
        banned, detail = probe.parse_ban(403, BAN_HTML)
        self.assertTrue(banned)
        self.assertIn("198.51.100.77", detail)
        self.assertTrue(probe.parse_ban(403, BAN_PLAIN)[0])

    def test_challenge_and_normal_pages_are_not_bans(self):
        banned, detail = probe.parse_ban(403, CHALLENGE)
        self.assertFalse(banned)
        self.assertIn("challenge", detail)
        self.assertEqual(probe.parse_ban(200, "<html>torrents</html>"), (False, "HTTP 200"))
        self.assertFalse(probe.parse_ban(200, "error code: 10060 in some log")[0])


class BanScheduleTests(unittest.TestCase):
    def setUp(self):
        probe._ban.update(ts=0.0, result=None)
        self.calls = []

    def fake_fetch(self, url):
        self.calls.append(url)
        return {"url": url, "status": 200, "banned": False, "detail": "HTTP 200", "ms": 50.0}

    def test_hourly_and_after_a_reconnect(self):
        conf = {"ban_urls": ["https://site.example/"], "ban_every": 3600, "recheck": 0.0}
        with mock.patch.object(probe, "fetch_ban", self.fake_fetch):
            probe.ban_check(conf, 1000.0)
            probe.ban_check(conf, 1030.0)
            probe.ban_check(conf, 4000.0)
            self.assertEqual(len(self.calls), 1)               # not again within the hour
            probe.ban_check(dict(conf, recheck=4010.0), 4030.0)  # the watchdog reconnected the VPN
            self.assertEqual(len(self.calls), 2)
            probe.ban_check(dict(conf, recheck=4010.0), 4060.0)
            self.assertEqual(len(self.calls), 2)
            probe.ban_check(conf, 4030.0 + 3600)
            self.assertEqual(len(self.calls), 3)

    def test_no_urls_no_check(self):
        with mock.patch.object(probe, "fetch_ban", self.fake_fetch):
            self.assertIsNone(probe.ban_check({"ban_urls": [], "ban_every": 3600, "recheck": 0}, 1000.0))
        self.assertEqual(self.calls, [])

    def test_conf_file(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "vpn.conf.json")
        with mock.patch.object(probe, "CONF", path):
            self.assertEqual(probe.read_conf(), {})
            dump({"ban_urls": ["https://site.example/", "ftp://nope", "javascript:x"], "ban_every": 5}, path)
            self.assertEqual(probe.read_conf(), {"ban_urls": ["https://site.example/"], "ban_every": 60, "recheck": 0.0})


class VpnBouncer(FakeBouncer):
    def __init__(self, works=True):
        super().__init__(works)
        self.vpn_restarts, self.vpn_result = [], 0

    def vpn_restart(self, tun):
        self.vpn_restarts.append(tun)
        if self.vpn_result:
            return self.vpn_result, f"{tun} NOT back after 60s (server was 192.0.2.1:501)"
        return 0, f"{tun} reconnected after 9s, server 192.0.2.1:501 -> 192.0.2.2:501"


class VpnMonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.dir = os.path.join(self.tmp, "probes")
        os.makedirs(self.dir)
        self.gw, self.clock = FakeGateway(), Clock()
        self.events = Events(os.path.join(self.tmp, "e.db"))
        self.settings = Settings(os.path.join(self.tmp, "s.json"), {
            "enabled": True, "dry_run": False, "probe_check": True, "vpn_check": True, "vpn_label": "MyVPN",
            "vpn_iface": "tunovpnc1", "vpn_ban_urls": "https://site.example/", "vpn_ban_minutes": 60,
            "vpn_down_minutes": 2, "vpn_cooldown_minutes": 5, "vpn_max_restarts_per_day": 24})
        self.history = Probes(os.path.join(self.tmp, "p.db"))
        self.bouncer = VpnBouncer()
        self.mon = Monitor(CFG, self.gw, self.events, self.settings, clock=self.clock, bouncer=self.bouncer,
                           probe_dir=self.dir, probe_history=self.history)
        self.mon.async_captures = False
        self.state, self.exit, self.banned, self.ban_ts = "ok", VPN_EXIT, False, None
        self.wan_probe = {"wan1": True, "wan2": True}
        self.write()
        self.mon.check()

    def write(self):
        ok = self.state == "ok"
        result = {"wan": "vpn", "ts": self.clock.t, "ip": "192.0.2.75", "interval": 30, "state": self.state,
                  "detail": "6/6 pings, 60 ms" if ok else "0/6 pings, Cloudflare unreachable",
                  "targets": [{"target": "1.1.1.1", "sent": 3, "recv": 3 if ok else 0, "avg_ms": 60.0 if ok else None}],
                  "trace": {"ok": ok, "exit_ip": self.exit if ok else None}}
        if ok:
            result["ban"] = {"ts": self.clock.t if self.ban_ts is None else self.ban_ts, "banned": self.banned,
                             "sites": [{"url": "https://site.example/", "status": 403 if self.banned else 200,
                                        "banned": self.banned, "detail": "x"}]}
        for key, ip in (("wan1", "203.0.113.10"), ("wan2", "198.51.100.20")):
            ok = self.wan_probe[key]
            dump({"wan": key, "ts": self.clock.t, "ip": "192.0.2.50", "interval": 30, "state": "ok" if ok else "fail",
                  "detail": "", "targets": [], "trace": {"ok": ok, "exit_ip": ip if ok else None}},
                 os.path.join(self.dir, f"{key}.json"))
        dump(result, os.path.join(self.dir, "vpn.json"))

    def run_for(self, minutes):
        for _ in range(int(minutes * 2)):
            self.clock.t += 30
            self.write()
            self.mon.check()

    def messages(self):
        return [e["message"] for e in self.events.recent(500)]

    def test_healthy_vpn_does_nothing(self):
        self.run_for(30)
        self.assertEqual(self.mon.vpn.state, "up")
        self.assertEqual(self.bouncer.vpn_restarts, [])
        self.assertTrue(any("MyVPN is up (exit IP 192.0.2.77)" in m for m in self.messages()))

    def test_ban_reconnects_right_away_then_rechecks(self):
        self.banned = True
        self.run_for(0.5)
        self.assertEqual(self.bouncer.vpn_restarts, ["tunovpnc1"])
        self.assertTrue(any("BANNED: site.example banned the exit IP" in m for m in self.messages()))
        self.assertTrue(any("Automatic reconnect of MyVPN: tunovpnc1 reconnected" in m for m in self.messages()))
        conf = load(os.path.join(self.dir, "vpn.conf.json"))
        self.assertEqual(conf["recheck"], self.mon.vpn.last_restart)      # probe asked to check the sites again
        self.assertEqual(conf["ban_every"], 3600)
        # the probe's next result still carries the ban check from before the reconnect: not a ban any more
        self.ban_ts = self.clock.t - 60
        self.run_for(1)
        self.assertEqual(self.mon.vpn.state, "up")
        self.assertIn("ban check pending", self.mon.vpn.note)
        self.assertEqual(len(self.bouncer.vpn_restarts), 1)

    def test_still_banned_waits_for_the_cooldown(self):
        self.banned = True
        self.run_for(0.5)
        self.run_for(4)                                   # new IP is banned too
        self.assertEqual(len(self.bouncer.vpn_restarts), 1)
        self.assertTrue(any("but waiting" in m and "cooldown" in m for m in self.messages()))
        self.run_for(1.5)
        self.assertEqual(len(self.bouncer.vpn_restarts), 2)

    def test_no_internet_for_two_minutes_reconnects(self):
        self.state = "fail"
        self.run_for(2)
        self.assertEqual(self.bouncer.vpn_restarts, [])
        self.run_for(1)
        self.assertEqual(self.bouncer.vpn_restarts, ["tunovpnc1"])
        self.assertTrue(any("MyVPN has NO INTERNET" in m for m in self.messages()))

    def test_waits_while_primary_wan_is_down(self):
        self.gw.wan["wan1"] = {"up": False, "ip": None}    # Verizon (the uplink) is down: fix that first
        self.wan_probe["wan1"] = False
        self.state = "fail"
        self.run_for(4)
        self.assertEqual(self.bouncer.vpn_restarts, [])
        self.assertIn("primary WAN Verizon is down", self.mon.vpn.action_note)

    def test_waits_while_a_wan_is_being_reset(self):
        self.mon.busy = {"wan": "wan2", "since": self.clock.t, "until": self.clock.t + 3600, "what": "power cycle"}
        self.gw.wan["wan2"] = {"up": False, "ip": None}
        self.banned = True
        self.run_for(1)
        self.assertEqual(self.bouncer.vpn_restarts, [])
        self.assertIn("AT&T is being reset first", self.mon.vpn.action_note)

    def test_leak_is_an_error_not_a_reconnect(self):
        self.exit = "203.0.113.10"                          # left through Verizon's own IP: kill switch broken
        self.run_for(5)
        self.assertEqual(self.mon.vpn.state, "leak")
        self.assertEqual(self.bouncer.vpn_restarts, [])
        self.assertEqual(sum("NOT going through the VPN" in m for m in self.messages()), 1)

    def test_dry_run_only_logs(self):
        self.settings.set(dry_run=True)
        self.banned = True
        self.run_for(10)
        self.assertEqual(self.bouncer.vpn_restarts, [])
        self.assertEqual(sum("DRY RUN: would reconnect MyVPN" in m for m in self.messages()), 2)   # cooldown 5 min

    def test_daily_limit(self):
        self.settings.set(vpn_max_restarts_per_day=2)
        self.banned = True
        self.run_for(30)
        self.assertEqual(len(self.bouncer.vpn_restarts), 2)
        self.assertIn("daily limit reached", self.mon.vpn.action_note)

    def test_failed_reconnect_is_logged_and_cools_down(self):
        self.bouncer.vpn_result = 3
        self.banned = True
        self.run_for(3)
        self.assertEqual(len(self.bouncer.vpn_restarts), 1)
        self.assertTrue(any("did NOT come back within 60 s" in m for m in self.messages()))

    def test_no_iface_or_no_access_never_reconnects(self):
        self.settings.set(vpn_iface="")
        self.banned = True
        self.run_for(3)
        self.assertEqual(self.bouncer.vpn_restarts, [])
        self.assertIn("no VPN tunnel interface", self.mon.vpn.action_note)

    def test_watch_off_only_shows(self):
        self.settings.set(vpn_check=False)
        self.banned = True
        self.run_for(5)
        self.assertEqual(self.mon.vpn.state, "off")
        self.assertEqual(self.bouncer.vpn_restarts, [])
        self.assertEqual(load(os.path.join(self.dir, "vpn.conf.json"))["ban_urls"], [])

    def test_manual_reconnect_acts_in_dry_run(self):
        self.settings.set(dry_run=True)
        ok, msg = self.mon.manual_vpn_restart()
        self.assertTrue(ok, msg)
        self.assertEqual(self.bouncer.vpn_restarts, ["tunovpnc1"])
        self.assertTrue(any("Manual reconnect of MyVPN" in m for m in self.messages()))

    def test_history_and_snapshot(self):
        self.run_for(1)
        self.assertEqual(sum(r["checks"] for r in self.history.hourly("vpn", 0)), 3)
        s = self.mon.snapshot()["vpn"]
        self.assertEqual((s["state"], s["exit_ip"], s["label"], s["iface"], s["watched"]),
                         ("up", VPN_EXIT, "MyVPN", "tunovpnc1", True))

    def test_wan_logic_untouched(self):
        self.banned = True
        self.run_for(10)
        self.assertEqual([w.state for w in self.mon.wans], ["up", "up"])
        self.assertEqual(self.gw.cycles, [])
        self.assertEqual(self.bouncer.bounces, [])


class VpnSettingsTests(unittest.TestCase):
    def test_validation(self):
        ok, errors = config.validate({"vpn_iface": "tunovpnc1", "vpn_ban_urls": "https://a.example/\n\nhttp://b.example"})
        self.assertEqual(errors, [])
        self.assertEqual(ok["vpn_ban_urls"], "https://a.example/\nhttp://b.example")
        self.assertEqual(config.ban_urls(ok), ["https://a.example/", "http://b.example"])
        for bad in ({"vpn_iface": "eth0"}, {"vpn_iface": "tun0; reboot"}, {"vpn_ban_urls": "at:AT+CFUN=0"},
                    {"vpn_ban_urls": "\n".join(f"https://{i}.example/" for i in range(5))}):
            self.assertTrue(config.validate(bad)[1], bad)
        self.assertEqual(config.validate({"vpn_iface": ""}), ({"vpn_iface": ""}, []))

    def test_hourly_by_default(self):
        self.assertEqual(config.FIELDS["vpn_ban_minutes"][3], 60)

    def test_env_seed(self):
        with mock.patch.dict(os.environ, {"VPN_IFACE": "tunovpnc1", "VPN_BAN_URLS": "https://a.example/, https://b.example/"}):
            v = config.initial_values()
        self.assertEqual((v["vpn_iface"], v["vpn_ban_urls"]), ("tunovpnc1", "https://a.example/\nhttps://b.example/"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
