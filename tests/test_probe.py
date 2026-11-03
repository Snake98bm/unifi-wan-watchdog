"""Probe output parsing and the watchdog's use of probe results. Run: python tests/test_probe.py"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from test_monitor import Clock, FakeBouncer, FakeGateway, CFG  # noqa: E402  (also stubs requests/urllib3)

import probe  # noqa: E402
from monitor import Monitor  # noqa: E402
from store import Events, Probes, Settings  # noqa: E402

BUSYBOX = """PING 1.1.1.1 (1.1.1.1): 56 data bytes

--- 1.1.1.1 ping statistics ---
3 packets transmitted, 2 packets received, 33% packet loss
round-trip min/avg/max = 41.347/43.004/44.745 ms
"""
IPUTILS = """--- 8.8.8.8 ping statistics ---
3 packets transmitted, 3 received, 0% packet loss, time 2002ms
rtt min/avg/max/mdev = 33.673/44.461/65.874/15.141 ms
"""
DEAD = """--- 1.1.1.1 ping statistics ---
3 packets transmitted, 0 packets received, 100% packet loss
"""


class ParseTests(unittest.TestCase):
    def test_busybox(self):
        self.assertEqual(probe.parse_ping(BUSYBOX), (2, 3, 43.004))

    def test_iputils(self):
        self.assertEqual(probe.parse_ping(IPUTILS), (3, 3, 44.461))

    def test_no_reply_and_garbage(self):
        self.assertEqual(probe.parse_ping(DEAD), (0, 3, None))
        self.assertEqual(probe.parse_ping("ping: sendto: Network unreachable"), (0, 3, None))

    def test_trace(self):
        body = "fl=12f1\nh=1.1.1.1\nip=198.51.100.20\nts=1730000000.1\nvisit_scheme=https\n"
        self.assertEqual(probe.parse_trace(body), "198.51.100.20")
        self.assertIsNone(probe.parse_trace("<html>error</html>"))

    def test_summarize(self):
        pings = [{"target": "1.1.1.1", "sent": 3, "recv": 0, "avg_ms": None},
                 {"target": "8.8.8.8", "sent": 3, "recv": 2, "avg_ms": 40.2}]
        state, detail = probe.summarize(pings, {"ok": True, "exit_ip": "198.51.100.20"})
        self.assertEqual(state, "ok")
        self.assertIn("2/6 pings", detail)
        state, detail = probe.summarize([dict(p, recv=0, avg_ms=None) for p in pings], {"ok": False, "exit_ip": None})
        self.assertEqual(state, "fail")
        self.assertIn("Cloudflare unreachable", detail)


class ProbeMonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.dir = os.path.join(self.tmp, "probes")
        os.makedirs(self.dir)
        self.gw, self.clock = FakeGateway(), Clock()
        self.events = Events(os.path.join(self.tmp, "e.db"))
        self.settings = Settings(os.path.join(self.tmp, "s.json"),
                                 {"enabled": True, "dry_run": False, "probe_check": True})
        self.history = Probes(os.path.join(self.tmp, "p.db"))
        self.bouncer = FakeBouncer()
        self.mon = Monitor(CFG, self.gw, self.events, self.settings, clock=self.clock, bouncer=self.bouncer,
                           probe_dir=self.dir, probe_history=self.history)
        self.mon.async_captures = False
        self.state = {"wan1": "ok", "wan2": "ok"}
        self.exit = {"wan1": "203.0.113.10", "wan2": "198.51.100.20"}   # each WAN's own public IP
        self.write()
        self.mon.check()

    def write(self, ts=None):
        for key, state in self.state.items():
            ok = state == "ok"
            result = {"wan": key, "ts": self.clock.t if ts is None else ts, "ip": "192.0.2.50", "interval": 30,
                      "state": state, "detail": "6/6 pings, 40 ms" if ok else "0/6 pings, Cloudflare unreachable",
                      "targets": [{"target": "1.1.1.1", "sent": 3, "recv": 3 if ok else 0, "avg_ms": 40.0 if ok else None}],
                      "trace": {"ok": ok, "exit_ip": self.exit[key] if ok else None}}
            with open(os.path.join(self.dir, f"{key}.json"), "w") as f:
                json.dump(result, f)

    def run_for(self, minutes):
        for _ in range(int(minutes * 2)):
            self.clock.t += 30
            self.write()
            self.mon.check()

    def wan(self, key):
        return self.mon._wan(key)

    def messages(self):
        return [e["message"] for e in self.events.recent(500)]

    def test_healthy_probe_is_up(self):
        self.run_for(2)
        self.assertEqual(self.wan("wan2").state, "up")
        self.assertEqual(self.wan("wan2").probe_verdict, "ok")

    def test_no_internet_goes_offline_then_bounces(self):
        self.state["wan2"] = "fail"
        self.run_for(0.5)
        self.assertEqual(self.wan("wan2").state, "up")         # one failed result is not enough
        self.run_for(0.5)
        self.assertEqual(self.wan("wan2").state, "offline")
        self.assertTrue(any("AT&T has NO INTERNET" in m for m in self.messages()))
        self.run_for(5)
        self.assertEqual(self.bouncer.bounces, ["eth0"])
        self.assertTrue(any("no internet for" in m for m in self.messages()))
        self.assertEqual(self.gw.cycles, [])
        self.assertEqual(self.wan("wan1").state, "up")

    def test_recovers_when_probe_answers(self):
        self.state["wan2"] = "fail"
        self.run_for(1.5)
        self.assertEqual(self.wan("wan2").state, "offline")
        self.state["wan2"] = "ok"
        self.run_for(0.5)
        self.assertEqual(self.wan("wan2").state, "up")
        self.assertIsNone(self.wan("wan2").down_since)

    def test_every_probe_failing_is_not_trusted(self):
        self.state = {"wan1": "fail", "wan2": "fail"}
        self.run_for(10)
        self.assertEqual(self.bouncer.bounces, [])
        self.assertEqual(self.gw.cycles, [])
        self.assertEqual({w.state for w in self.mon.wans}, {"up"})
        self.assertEqual(sum("Every internet probe is failing" in m for m in self.messages()), 1)

    def test_other_wan_down_does_not_hide_a_dead_probe(self):
        self.gw.wan["wan1"] = {"up": False, "ip": None}    # Verizon link down: its probe can't count
        self.state = {"wan1": "fail", "wan2": "fail"}
        self.run_for(1.5)
        self.assertEqual(self.wan("wan2").state, "offline")

    def test_wrong_path_is_ignored_and_logged_once(self):
        self.exit["wan2"] = self.exit["wan1"]               # AT&T's probe came out through Verizon
        self.run_for(3)
        self.assertEqual(self.wan("wan2").probe_verdict, "wrong-path")
        self.assertEqual(self.wan("wan2").state, "up")
        self.assertEqual(sum("is not testing AT&T" in m for m in self.messages()), 1)

    def test_stale_result_is_ignored(self):
        self.state["wan2"] = "fail"
        for _ in range(12):
            self.clock.t += 30
            self.write(ts=self.clock.t - 600)               # probe stopped 10 min ago
            self.mon.check()
        self.assertEqual(self.wan("wan2").probe_verdict, "unknown")
        self.assertEqual(self.wan("wan2").state, "up")

    def test_same_result_read_twice_counts_once(self):
        self.state["wan2"] = "fail"
        self.clock.t += 30
        self.write()
        for _ in range(3):                                  # watchdog checks faster than the probe writes
            self.mon.check()
        self.assertEqual(self.wan("wan2").probe_fails, 1)
        self.assertEqual(self.wan("wan2").state, "up")

    def test_check_off_only_shows(self):
        self.settings.set(probe_check=False)
        self.state["wan2"] = "fail"
        self.run_for(10)
        self.assertEqual(self.wan("wan2").probe_verdict, "off")
        self.assertEqual(self.wan("wan2").state, "up")
        self.assertEqual(self.bouncer.bounces, [])

    def test_unifi_restart_with_empty_wan_data_is_not_an_outage(self):
        self.run_for(1)
        self.gw.wan = {"wan1": {}, "wan2": {"up": False, "ip": None}}   # what a restarting Network app returns
        self.run_for(10)
        self.assertEqual([w.state for w in self.mon.wans], ["up", "up"])
        self.assertEqual(self.bouncer.bounces, [])
        self.assertEqual(sum("treating UniFi's data as stale" in m for m in self.messages()), 2)
        self.gw.wan = {"wan1": {"up": True, "ip": "203.0.113.10"}, "wan2": {"up": True, "ip": "198.51.100.20"}}
        self.run_for(0.5)
        self.assertEqual(sum("looks normal again" in m for m in self.messages()), 2)
        self.assertFalse(any("is DOWN" in m for m in self.messages()))

    def test_real_outage_still_counts_when_the_probe_fails_too(self):
        self.run_for(1)
        self.gw.wan["wan2"] = {"up": False, "ip": None}
        self.state["wan2"] = "fail"
        self.run_for(0.5)
        self.assertEqual(self.wan("wan2").state, "down")
        self.run_for(5)
        self.assertEqual(self.bouncer.bounces, ["eth0"])

    def test_probe_from_another_ip_does_not_vouch(self):
        self.run_for(1)
        self.exit["wan2"] = "192.0.2.99"                     # got out, but not through AT&T's address
        self.gw.wan["wan2"] = {"up": False, "ip": None}
        self.run_for(0.5)
        self.assertEqual(self.wan("wan2").state, "down")

    def test_pages_list_wans_by_number(self):
        s = self.mon.snapshot()
        self.assertEqual([(w["key"], w["name"]) for w in s["wans"]], [("wan1", "WAN 1"), ("wan2", "WAN 2")])
        self.assertEqual(s["first_label"], "AT&T")            # reset priority is unchanged
        self.assertEqual(self.mon.wans[0].key, "wan2")

    def test_history_by_hour(self):
        self.run_for(2)
        self.state["wan2"] = "fail"
        self.run_for(1)
        rows = self.history.hourly("wan2", 0)
        self.assertEqual(sum(r["checks"] for r in rows), 7)    # first check + 4 ok + 2 failed
        self.assertEqual(sum(r["failed"] for r in rows), 2)
        self.assertEqual(self.history.hourly("wan1", 0)[0]["loss"], 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
