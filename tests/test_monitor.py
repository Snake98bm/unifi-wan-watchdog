"""Decision-logic tests with a fake gateway and a fake clock (no network). Run: python tests/test_monitor.py"""
import os
import sys
import tempfile
import threading
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
# monitor imports unifi, which imports requests/urllib3; stub them so the logic runs anywhere
for name in ("requests", "urllib3"):
    if name not in sys.modules:
        stub = types.ModuleType(name)
        stub.Session = object
        stub.RequestException = Exception
        stub.exceptions = types.SimpleNamespace(InsecureRequestWarning=Warning)
        stub.disable_warnings = lambda *a: None
        sys.modules[name] = stub

from monitor import Monitor  # noqa: E402
from store import Events, Settings  # noqa: E402

CFG = {"wans": [("wan2", "AT&T", 1), ("wan1", "Verizon", 8)], "check_interval": 30, "down_minutes": 5,
       "private_minutes": 5, "recovery_minutes": 10, "cooldown_minutes": 30, "max_cycles_per_day": 6,
       "bounce_wait_minutes": 3, "bounce_check_minutes": 60}


class FakeBouncer:
    def __init__(self, works=True):
        self.works, self.bounces, self.checks = works, [], 0

    def configured(self):
        return True

    def check(self):
        self.checks += 1
        return (True, "UDMPROSE.v5.1.34") if self.works else (False, "Permission denied (publickey)")

    result = None      # override the exit code of the next bounces (e.g. 3 = link not back)

    def wanlog(self, ifname):
        self.wanlogs = getattr(self, "wanlogs", []) + [ifname]
        return 0, (f"2026-11-03T03:00:26-08:00 UDM ubios-udapi-server[2384]: dhcp-client: Suspending "
                   f"udhcpc-{ifname}\n")

    def bounce(self, ifname):
        self.bounces.append(ifname)
        if not self.works:
            return 255, "Permission denied (publickey)"
        if self.result is not None:
            return self.result, f"{ifname} still DOWN after 30s (link down 5 s)"
        return 0, f"{ifname} back UP at 1000 Mb/s after 6s (link down 5 s)"


class FakeRebootCapture:
    def __init__(self, ok=True):
        self.ok, self.reboots = ok, []

    def has(self, key):
        return False

    def can_reboot(self, key):
        return True

    def reboot(self, key):
        self.reboots.append(key)
        if not self.ok:
            raise RuntimeError("box unreachable")


class FakeProbes:
    def __init__(self, avg=None):
        self.avg = avg

    def recent(self, wan, since):
        return (10, self.avg, 0)


# what the real gateway reports (trimmed): AT&T = wan2 on port 1 / eth0, Verizon = wan on port 8 / eth7
ATT_MODEM = [{"ip": "198.51.100.1", "mac": "88:96:4e:00:00:01"}]
VZ_MODEM = [{"ip": "203.0.113.9", "mac": "02:00:00:00:00:02"}]   # locally administered: not fingerprinted


class FakeGateway:
    def __init__(self):
        self.wan = {"wan1": {"up": True, "ip": "203.0.113.10"}, "wan2": {"up": True, "ip": "198.51.100.20"}}
        self.wan_extra = {"wan1": {"ifname": "eth7", "port_idx": 8, "is_uplink": True, "mac_table": VZ_MODEM},
                          "wan2": {"ifname": "eth0", "port_idx": 1, "mac_table": ATT_MODEM}}
        self.ports = {1: {"port_idx": 1, "up": True, "ifname": "eth0", "network_name": "wan2", "poe_class": "Class 4"},
                      8: {"port_idx": 8, "up": True, "ifname": "eth7", "network_name": "wan", "poe_class": "Class 0"}}
        self.cycles = []

    version = "5.1.34"

    def gateway(self):
        return {"mac": "aa:bb:cc:00:00:01", "version": self.version,
                "wan1": {**self.wan_extra["wan1"], **self.wan["wan1"]},
                "wan2": {**self.wan_extra["wan2"], **self.wan["wan2"]},
                "port_table": [dict(p) for p in self.ports.values()]}

    def swap_cables(self):
        """Verizon's modem now on port 1 and AT&T's on port 8, with UniFi's WAN config moved to match."""
        self.ports[1].update(network_name="wan", poe_class="Class 0")
        self.ports[8].update(network_name="wan2", poe_class="Class 4")
        self.wan_extra["wan1"].update(ifname="eth0", port_idx=1)
        self.wan_extra["wan2"].update(ifname="eth7", port_idx=8)

    def power_cycle(self, mac, port):
        self.cycles.append(port)
        return "fake API"


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t

    def run(self, mon, minutes):
        for _ in range(int(minutes * 2)):  # one check every 30 s
            self.t += 30
            mon.check()


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.gw, self.clock = FakeGateway(), Clock()
        self.events = Events(os.path.join(self.tmp, "e.db"))
        self.settings = Settings(os.path.join(self.tmp, "s.json"), {"enabled": True, "dry_run": False})
        self.mon = Monitor(CFG, self.gw, self.events, self.settings, clock=self.clock)
        self.mon.async_captures = False
        self.mon.check()

    def test_healthy_does_nothing(self):
        self.clock.run(self.mon, 30)
        self.assertEqual(self.gw.cycles, [])

    def test_short_drop_is_ignored(self):  # a modem's own ~1 min reboot
        self.gw.wan["wan1"] = {"up": False, "ip": None}
        self.clock.run(self.mon, 2)
        self.gw.wan["wan1"] = {"up": True, "ip": "203.0.113.10"}
        self.clock.run(self.mon, 10)
        self.assertEqual(self.gw.cycles, [])

    def test_down_five_minutes_cycles_once(self):
        self.gw.wan["wan1"] = {"up": False, "ip": None}
        self.clock.run(self.mon, 4.5)
        self.assertEqual(self.gw.cycles, [])
        self.clock.run(self.mon, 1)
        self.assertEqual(self.gw.cycles, [8])
        self.clock.run(self.mon, 20)  # still down, but cooldown (30 min) holds
        self.assertEqual(self.gw.cycles, [8])

    def test_private_ip_rechecked_then_cycled(self):
        self.gw.wan["wan1"] = {"up": True, "ip": "192.168.225.44"}
        self.clock.run(self.mon, 4.5)
        self.assertEqual(self.gw.cycles, [])
        self.clock.run(self.mon, 1)
        self.assertEqual(self.gw.cycles, [8])

    def test_private_ip_that_clears_is_left_alone(self):
        self.gw.wan["wan1"] = {"up": True, "ip": "192.168.225.44"}
        self.clock.run(self.mon, 3)
        self.gw.wan["wan1"] = {"up": True, "ip": "203.0.113.10"}
        self.clock.run(self.mon, 10)
        self.assertEqual(self.gw.cycles, [])

    def test_both_bad_att_first_and_never_together(self):
        self.gw.wan["wan1"] = {"up": False, "ip": None}
        self.gw.wan["wan2"] = {"up": False, "ip": None}
        self.clock.run(self.mon, 5.5)
        self.assertEqual(self.gw.cycles, [1])         # AT&T only
        self.clock.run(self.mon, 9)                   # inside AT&T's 10 min recovery window
        self.assertEqual(self.gw.cycles, [1])         # Verizon still waits
        self.clock.run(self.mon, 2)                   # window over, AT&T still down
        self.assertEqual(self.gw.cycles, [1, 8])      # now Verizon, alone

    def test_verizon_waits_until_att_recovers(self):
        self.gw.wan["wan1"] = {"up": False, "ip": None}
        self.gw.wan["wan2"] = {"up": False, "ip": None}
        self.clock.run(self.mon, 5.5)
        self.gw.wan["wan2"] = {"up": True, "ip": "198.51.100.20"}   # AT&T comes back
        self.clock.run(self.mon, 1)                   # < 2 min after the cycle: still busy
        self.assertEqual(self.gw.cycles, [1])
        self.clock.run(self.mon, 1.5)                 # AT&T healthy and >= 2 min: Verizon may go
        self.assertEqual(self.gw.cycles, [1, 8])

    def test_manual_cycle_refused_while_other_recovers(self):
        ok, _ = self.mon.manual_cycle("wan2")
        self.assertTrue(ok)
        ok, msg = self.mon.manual_cycle("wan1")
        self.assertFalse(ok)
        self.assertIn("one WAN at a time", msg)
        self.assertEqual(self.gw.cycles, [1])

    def test_daily_cap(self):
        self.gw.wan["wan2"] = {"up": False, "ip": None}
        self.clock.run(self.mon, 60 * 6)              # 6 hours of AT&T down
        self.assertEqual(len(self.gw.cycles), CFG["max_cycles_per_day"])

    def test_dry_run_touches_nothing(self):
        self.settings.set(dry_run=True)
        self.gw.wan["wan1"] = {"up": False, "ip": None}
        self.clock.run(self.mon, 6)
        self.assertEqual(self.gw.cycles, [])
        self.assertTrue(any("DRY RUN" in e["message"] for e in self.events.recent()))

    def test_modem_pages_saved_before_att_cycle(self):
        order = []

        class FakeCapture:
            def has(self, key):
                return key == "wan2"

            def __call__(self, key, reason="", extra=None):
                order.append(("capture", key, reason))
                return "20261103-120000-wan2", 6, []

        self.mon.capture = FakeCapture()
        real_cycle = self.gw.power_cycle
        self.gw.power_cycle = lambda mac, port: (order.append(("cycle", port)), real_cycle(mac, port))[1]
        self.gw.wan["wan2"] = {"up": False, "ip": None}
        self.clock.run(self.mon, 6)
        # saved once when it went down (after ~30 s), then again right before the power cycle
        self.assertEqual(order, [("capture", "wan2", "(went down)"), ("capture", "wan2", "before the power cycle"),
                                 ("cycle", 1)])

    def test_event_capture_once_an_hour_and_not_on_a_blip(self):
        reasons = []

        class FakeCapture:
            def has(self, key):
                return True

            def __call__(self, key, reason="", extra=None):
                reasons.append((key, reason))
                return "x", 1, []

        self.mon.capture = FakeCapture()
        self.settings.set(enabled=False)                       # only watch, so no power-cycle captures
        self.gw.wan["wan2"] = {"up": False, "ip": None}         # 30 s blip: one bad check
        self.mon.check()
        self.gw.wan["wan2"] = {"up": True, "ip": "198.51.100.20"}
        self.mon.check()
        self.assertEqual(reasons, [])
        for _ in range(4):                                       # flapping: down a minute, up, ...
            self.gw.wan["wan2"] = {"up": False, "ip": None}
            self.clock.run(self.mon, 1)
            self.gw.wan["wan2"] = {"up": True, "ip": "198.51.100.20"}
            self.clock.run(self.mon, 1)
        self.assertEqual(reasons, [("wan2", "(went down)")])  # once, not every flap
        self.clock.run(self.mon, 60)
        self.gw.wan["wan1"] = {"up": True, "ip": "192.168.225.44"}
        self.gw.wan["wan2"] = {"up": False, "ip": None}
        self.clock.run(self.mon, 1)
        self.assertEqual(reasons[1:], [("wan2", "(went down)"), ("wan1", "(went private)")])

    def test_gateway_log_saved_with_capture(self):
        self.with_bouncer()
        got = {}

        class FakeCapture:
            def has(self, key):
                return True

            def __call__(self, key, reason="", extra=None):
                got[key] = extra
                return "x", 1, []

        self.mon.capture = FakeCapture()
        self.mon.save_pages(self.mon._wan("wan2"), "test")
        page, html = got["wan2"][0]
        self.assertEqual(page, "gateway_wan_log")
        self.assertIn("Suspending udhcpc-eth0", html)
        self.assertEqual(self.bouncer.wanlogs, ["eth0"])

    def test_capture_failure_does_not_block_cycle(self):
        class Broken:
            def has(self, key):
                return True

            def __call__(self, key, reason="", extra=None):
                raise OSError("modem unreachable")

        self.mon.capture = Broken()
        self.gw.wan["wan2"] = {"up": False, "ip": None}
        self.clock.run(self.mon, 6)
        self.assertEqual(self.gw.cycles, [1])

    # ---------- port bounce ----------
    def with_bouncer(self, works=True):
        self.bouncer = FakeBouncer(works)
        self.mon = Monitor(CFG, self.gw, self.events, self.settings, clock=self.clock, bouncer=self.bouncer)
        self.mon.async_captures = False
        self.mon.check()

    def test_bounce_first_then_cycle_if_still_bad(self):
        self.with_bouncer()
        self.gw.wan["wan1"] = {"up": False, "ip": None}
        self.clock.run(self.mon, 5.5)
        self.assertEqual((self.bouncer.bounces, self.gw.cycles), (["eth7"], []))
        self.clock.run(self.mon, 2.5)                 # within the 3 min bounce wait
        self.assertEqual(self.gw.cycles, [])
        self.clock.run(self.mon, 1)                   # still down after the wait
        self.assertEqual((self.bouncer.bounces, self.gw.cycles), (["eth7"], [8]))

    def test_bounce_that_fixes_it_means_no_cycle(self):
        self.with_bouncer()
        self.gw.wan["wan1"] = {"up": True, "ip": "192.168.225.44"}
        self.clock.run(self.mon, 5.5)
        self.assertEqual(self.bouncer.bounces, ["eth7"])
        self.gw.wan["wan1"] = {"up": True, "ip": "203.0.113.10"}
        self.clock.run(self.mon, 20)
        self.assertEqual(self.gw.cycles, [])

    def test_bounce_one_wan_at_a_time_att_first(self):
        self.with_bouncer()
        self.gw.wan["wan1"] = {"up": False, "ip": None}
        self.gw.wan["wan2"] = {"up": False, "ip": None}
        self.clock.run(self.mon, 5.5)
        self.assertEqual(self.bouncer.bounces, ["eth0"])     # AT&T only
        self.clock.run(self.mon, 2.5)
        self.assertEqual(self.bouncer.bounces, ["eth0"])     # Verizon waits during AT&T's bounce window
        self.clock.run(self.mon, 1)
        # window over: AT&T is next in line (its PoE cycle), Verizon still waits its turn
        self.assertEqual((self.bouncer.bounces, self.gw.cycles), (["eth0"], [1]))

    def test_failed_bounce_goes_straight_to_cycle(self):
        self.with_bouncer()
        self.bouncer.works = False                    # e.g. key wiped after the hourly check
        self.gw.wan["wan2"] = {"up": False, "ip": None}
        self.clock.run(self.mon, 6)
        self.assertEqual((self.bouncer.bounces, self.gw.cycles), (["eth0"], [1]))

    def test_no_bounce_access_uses_poe_cycle(self):
        self.with_bouncer(works=False)
        self.gw.wan["wan2"] = {"up": False, "ip": None}
        self.clock.run(self.mon, 6)
        self.assertEqual((self.bouncer.bounces, self.gw.cycles), ([], [1]))

    def test_firmware_upgrade_survival_logged(self):
        self.with_bouncer()
        checks = self.bouncer.checks
        self.gw.version = "5.2.0"
        self.mon.check()
        self.assertEqual(self.bouncer.checks, checks + 1)   # checked right after the change
        self.assertTrue(any("SURVIVED" in e["message"] for e in self.events.recent()))

    def test_firmware_upgrade_lost_access_logged(self):
        self.with_bouncer()
        self.bouncer.works = False
        self.gw.version = "5.2.0"
        self.mon.check()
        self.assertTrue(any("was LOST" in e["message"] for e in self.events.recent()))
        self.assertFalse(self.mon.bounce_access["ok"])

    def _gateway_outage(self, minutes, new_version=None):
        from unifi import UniFiError
        real = self.gw.gateway
        msgs = iter(["HTTP 502", "Connection refused", "timed out", "Connection refused", "not JSON"])

        def down():
            raise UniFiError("GET /stat/device: " + next(msgs, "not JSON"))
        self.gw.gateway = down
        self.clock.run(self.mon, minutes)
        self.gw.gateway = real
        if new_version:
            self.gw.version = new_version
        self.clock.run(self.mon, 0.5)

    def test_firmware_upgrade_outage_relabeled(self):
        self._gateway_outage(12, new_version="5.1.35")
        log = self.events.recent()
        self.assertEqual([e for e in log if e["level"] == "error"], [])          # no red rows left
        gw_errs = [e for e in log if e["message"].startswith("Can't read the gateway")]
        self.assertEqual((len(gw_errs), {e["level"] for e in gw_errs}), (5, {"warn"}))
        summary = [e for e in log if "rebooting for a firmware upgrade 5.1.34 -> 5.1.35" in e["message"]]
        self.assertEqual(len(summary), 1)
        self.assertIn("about 12 min", summary[0]["message"])
        self.assertIn("The 5 error(s)", summary[0]["message"])
        self.assertIsNone(self.mon._outage)

    def test_outage_without_upgrade_stays_error(self):
        self._gateway_outage(3)
        log = self.events.recent()
        self.assertEqual(len([e for e in log if e["level"] == "error"]), 5)
        self.assertFalse(any("firmware upgrade" in e["message"] for e in log))
        self.gw.version = "5.1.35"                    # a later upgrade with no outage doesn't touch them
        self.mon.check()
        self.assertEqual(len([e for e in self.events.recent() if e["level"] == "error"]), 5)

    def test_bounce_access_checked_at_startup_only(self):
        self.with_bouncer()
        self.assertEqual(self.bouncer.checks, 1)              # at startup
        self.clock.run(self.mon, 25 * 60)                      # a day later: not checked again
        self.assertEqual(self.bouncer.checks, 1)
        self.gw.version = "5.2.0"                               # ... until the firmware changes
        self.mon.check()
        self.assertEqual(self.bouncer.checks, 2)

    def test_buttons_really_act_in_dry_run(self):
        self.with_bouncer()
        self.settings.set(dry_run=True)
        ok, _ = self.mon.manual_bounce("wan2")
        self.assertTrue(ok)
        self.assertEqual(self.bouncer.bounces, ["eth0"])
        self.mon.busy = None                          # skip AT&T's wait for this test
        ok, _ = self.mon.manual_cycle("wan1")
        self.assertTrue(ok)
        self.assertEqual(self.gw.cycles, [8])

    def test_manual_bounce_never_leads_to_a_power_cycle(self):
        self.with_bouncer()
        self.gw.wan["wan2"] = {"up": False, "ip": None}
        self.clock.run(self.mon, 4)                   # down 4 min, automation hasn't acted yet
        ok, _ = self.mon.manual_bounce("wan2")
        self.assertTrue(ok)
        self.clock.run(self.mon, 4.5)                 # still down: the manual bounce restarted the timers
        self.assertEqual((self.bouncer.bounces, self.gw.cycles), (["eth0"], []))
        self.clock.run(self.mon, 1)                   # now the automation's OWN bounce (5 min after the click)
        self.assertEqual((self.bouncer.bounces, self.gw.cycles), (["eth0", "eth0"], []))
        self.clock.run(self.mon, 3.5)                 # ... and only after that one fails, its PoE backup
        self.assertEqual(self.gw.cycles, [1])

    def test_failed_manual_bounce_does_not_trigger_cycle(self):
        self.with_bouncer()
        self.bouncer.result = 3
        self.gw.wan["wan2"] = {"up": False, "ip": None}
        self.clock.run(self.mon, 4.5)
        self.mon.manual_bounce("wan2")
        self.clock.run(self.mon, 1)
        self.assertEqual(self.gw.cycles, [])

    def test_buttons_still_one_wan_at_a_time_in_dry_run(self):
        self.with_bouncer()
        self.settings.set(dry_run=True)
        self.mon.manual_bounce("wan2")
        ok, msg = self.mon.manual_cycle("wan1")
        self.assertFalse(ok)
        self.assertIn("one WAN at a time", msg)

    def test_automatic_still_dry_in_dry_run(self):
        self.with_bouncer()
        self.settings.set(dry_run=True)
        self.gw.wan["wan2"] = {"up": False, "ip": None}
        self.clock.run(self.mon, 12)
        self.assertEqual((self.bouncer.bounces, self.gw.cycles), ([], []))

    # ---------- safety checks ----------
    def test_fingerprint_learned_when_healthy(self):
        self.assertEqual(self.settings.get("wan2_fingerprint"), {"poe_class": "Class 4", "vendor": "88:96:4e"})
        self.assertEqual(self.settings.get("wan1_fingerprint"), {"poe_class": "Class 0"})   # Verizon MAC changes

    def test_swapped_cables_refuse_automatic_and_manual(self):
        self.gw.swap_cables()
        self.gw.wan["wan2"] = {"up": False, "ip": None}
        self.clock.run(self.mon, 10)
        self.assertEqual(self.gw.cycles, [])
        att = self.mon._wan("wan2")
        self.assertTrue(any("PoE Class 4 -> Class 0" in p or "port 8" in p for p in att.problems), att.problems)
        ok, msg = self.mon.manual_cycle("wan2")
        self.assertFalse(ok)
        self.assertIn("safety check failed", msg)
        logged = [e["message"] for e in self.events.recent() if e["message"].startswith("Not touching AT&T")]
        self.assertEqual(len(logged), 1)                   # logged once, not every 30 s

    def test_port_moved_to_other_wan_network_refused(self):
        self.gw.ports[1]["network_name"] = "lan"              # port 1 is no longer a WAN port
        self.mon.check()
        ok, msg = self.mon.manual_cycle("wan2")
        self.assertFalse(ok)
        self.assertIn("belongs to 'lan', not WAN2", msg)

    def test_settings_port_disagrees_with_unifi_refused(self):
        self.gw.wan_extra["wan2"]["port_idx"] = 2             # UniFi says AT&T moved to port 2
        self.mon.check()
        self.assertIn("UniFi says AT&T is on port 2", " ".join(self.mon._wan("wan2").problems))

    def test_swapped_but_other_wan_still_handled(self):
        self.gw.ports[1]["poe_class"] = "Class 2"           # AT&T's port looks wrong ...
        self.gw.wan["wan1"] = {"up": False, "ip": None}      # ... Verizon needs a reset and is fine to touch
        self.gw.wan["wan2"] = {"up": False, "ip": None}
        self.clock.run(self.mon, 6)
        self.assertEqual(self.gw.cycles, [8])

    def test_relearn_accepts_new_hardware(self):
        self.gw.ports[1]["poe_class"] = "Class 3"            # new AT&T modem on purpose
        self.mon.check()
        self.assertTrue(self.mon._wan("wan2").problems)
        self.mon.relearn("wan2")
        self.mon.check()
        self.assertEqual(self.mon._wan("wan2").problems, [])
        self.assertEqual(self.settings.get("wan2_fingerprint")["poe_class"], "Class 3")

    # ---------- bounce verification ----------
    def test_bounce_link_not_back_goes_to_cycle_access_stays_ok(self):
        self.with_bouncer()
        self.bouncer.result = 3
        self.gw.wan["wan2"] = {"up": False, "ip": None}
        self.clock.run(self.mon, 6)
        self.assertEqual((self.bouncer.bounces, self.gw.cycles), (["eth0"], [1]))
        self.assertTrue(self.mon.bounce_access["ok"])
        self.assertTrue(any("did NOT come back up" in e["message"] for e in self.events.recent()))

    def test_manual_bounce_reports_verification(self):
        self.with_bouncer()
        ok, msg = self.mon.manual_bounce("wan2")
        self.assertTrue(ok)
        self.assertEqual(msg, "Bounce verified: eth0 back UP at 1000 Mb/s after 6s (link down 5 s)")

    # ---------- review fixes ----------
    def test_lock_is_free_while_bounce_runs(self):
        self.with_bouncer()
        mon, seen = self.mon, {}

        def slow_bounce(ifname):
            t = threading.Thread(target=lambda: seen.update(snap=mon.snapshot()))
            t.start()
            t.join(2)                                       # would hang if the lock were held
            seen["other"] = mon.manual_cycle("wan1")        # slot is reserved: refused
            return 0, f"{ifname} back UP"

        self.bouncer.bounce = slow_bounce
        ok, _ = mon.manual_bounce("wan2")
        self.assertTrue(ok)
        self.assertIn("snap", seen)
        self.assertTrue(seen["snap"]["busy"]["pending"])
        self.assertFalse(seen["other"][0])
        self.assertIn("one WAN at a time", seen["other"][1])

    def test_transient_ssh_failure_keeps_bounce_access(self):
        self.with_bouncer()
        self.bouncer.bounce = lambda ifname: (255, "ssh: connect to host timed out")   # check() still works
        self.mon.manual_bounce("wan2")
        self.assertTrue(self.mon.bounce_access["ok"])

    def test_lost_bounce_access_is_retried_until_it_works(self):
        self.with_bouncer()
        self.bouncer.works = False
        self.mon.manual_bounce("wan2")                      # 255 and check() fails too -> lost
        self.assertEqual((self.mon.bounce_access["ok"], self.mon.bounce_access["lost"]), (False, True))
        self.bouncer.works = True
        self.clock.run(self.mon, 29)
        self.assertFalse(self.mon.bounce_access["ok"])       # not hammered every 30 s
        self.clock.run(self.mon, 2)
        self.assertTrue(self.mon.bounce_access["ok"])        # re-checked after 30 min and back

    def test_never_installed_bounce_is_not_retried(self):
        self.with_bouncer(works=False)
        checks = self.bouncer.checks
        self.clock.run(self.mon, 120)
        self.assertEqual(self.bouncer.checks, checks)

    def test_dry_run_cycles_dont_use_up_real_limit(self):
        self.settings.set(dry_run=True)
        self.gw.wan["wan2"] = {"up": False, "ip": None}
        self.clock.run(self.mon, 60 * 6)                     # 6 h of simulated cycles
        self.assertEqual(self.mon._wan("wan2").cycles_today, 0)
        self.settings.set(dry_run=False)
        self.clock.run(self.mon, 6)
        self.assertEqual(self.gw.cycles, [1])                 # real cycle not blocked by the dry ones

    def test_failed_power_cycle_frees_the_slot(self):
        from unifi import UniFiError

        def boom(mac, port):
            raise UniFiError("timeout")
        self.gw.power_cycle = boom
        ok, _ = self.mon.manual_cycle("wan2")
        self.assertFalse(ok)
        self.assertIsNone(self.mon.busy)

    # ---------- modem reboot ----------
    def _reboot_mon(self, cap_ok=True, avg=None):
        cap, probes = FakeRebootCapture(cap_ok), FakeProbes(avg)
        mon = Monitor(CFG, self.gw, self.events, self.settings, clock=self.clock, capture=cap, probe_history=probes)
        mon.async_captures = False
        mon.check()
        return mon, cap

    def test_reboot_box_holds_the_slot_so_no_bounce_during_reboot(self):
        mon, cap = self._reboot_mon()
        ok, _ = mon.reboot_box("wan2", "test", manual=True)
        self.assertTrue(ok)
        self.assertEqual(cap.reboots, ["wan2"])
        self.assertEqual(mon.busy["what"], "modem reboot")      # recovery window reserved

    def test_reboot_box_dry_run_only_logs(self):
        self.settings.set(dry_run=True)
        mon, cap = self._reboot_mon()
        ok, _ = mon.reboot_box("wan2", "test")                  # automatic -> dry run
        self.assertTrue(ok)
        self.assertEqual(cap.reboots, [])
        self.assertIsNone(mon.busy)
        self.assertTrue(any("DRY RUN" in e["message"] and "reboot" in e["message"] for e in self.events.recent()))

    def test_reboot_box_refused_while_busy(self):
        mon, cap = self._reboot_mon()
        mon.busy = {"wan": "wan1", "since": 0, "what": "power cycle", "until": 0}
        ok, _ = mon.reboot_box("wan2", "test", manual=True)
        self.assertFalse(ok)
        self.assertEqual(cap.reboots, [])

    def test_reboot_box_failure_frees_the_slot(self):
        mon, cap = self._reboot_mon(cap_ok=False)
        ok, _ = mon.reboot_box("wan2", "test", manual=True)
        self.assertFalse(ok)
        self.assertIsNone(mon.busy)
        self.assertTrue(any("Couldn't reboot" in e["message"] for e in self.events.recent()))

    def test_box_reboot_tick_high_latency_reboots_once_a_day(self):
        import datetime
        self.settings.set(box_reboot=True, box_reboot_wan="wan2", box_reboot_hour=4, box_reboot_latency_ms=90)
        mon, cap = self._reboot_mon(avg=150)
        at4 = datetime.datetime(2026, 11, 5, 4, 0)
        self.assertTrue(mon.box_reboot_tick(at4))
        self.assertEqual(cap.reboots, ["wan2"])
        self.assertFalse(mon.box_reboot_tick(at4))              # same day: not twice
        self.assertEqual(cap.reboots, ["wan2"])

    def test_box_reboot_skips_a_disabled_wan(self):
        import datetime
        self.settings.set(box_reboot=True, box_reboot_wan="wan2", box_reboot_hour=4, box_reboot_latency_ms=0,
                          wan2_enabled=False)
        mon, cap = self._reboot_mon(avg=200)
        self.assertFalse(mon.box_reboot_tick(datetime.datetime(2026, 11, 5, 4, 0)))
        self.assertEqual(cap.reboots, [])                 # disabled WAN -> modem left alone
        self.assertEqual(mon.snapshot()["box_reboot"]["wan_enabled"], False)

    def test_box_reboot_tick_low_latency_skips(self):
        import datetime
        self.settings.set(box_reboot=True, box_reboot_hour=4, box_reboot_latency_ms=90)
        mon, cap = self._reboot_mon(avg=35)
        self.assertFalse(mon.box_reboot_tick(datetime.datetime(2026, 11, 5, 4, 0)))
        self.assertEqual(cap.reboots, [])
        self.assertTrue(any("latency OK" in e["message"] for e in self.events.recent()))

    def test_box_reboot_tick_off_or_wrong_hour_does_nothing(self):
        import datetime
        self.settings.set(box_reboot=True, box_reboot_hour=4, box_reboot_latency_ms=0)
        mon, cap = self._reboot_mon()
        self.assertFalse(mon.box_reboot_tick(datetime.datetime(2026, 11, 5, 9, 0)))   # wrong hour
        self.settings.set(box_reboot=False)
        self.assertFalse(mon.box_reboot_tick(datetime.datetime(2026, 11, 5, 4, 0)))   # disabled
        self.assertEqual(cap.reboots, [])

    def test_box_reboot_tick_threshold_zero_reboots_regardless(self):
        import datetime
        self.settings.set(box_reboot=True, box_reboot_hour=3, box_reboot_latency_ms=0)
        mon, cap = self._reboot_mon(avg=None)                  # no probe data needed when threshold is 0
        self.assertTrue(mon.box_reboot_tick(datetime.datetime(2026, 11, 5, 3, 0)))
        self.assertEqual(cap.reboots, ["wan2"])

    # ---------- per-WAN enable ----------
    def test_disabled_wan_is_never_cycled_or_alerted(self):
        self.settings.set(wan1_enabled=False)
        self.gw.wan["wan1"] = {"up": False, "ip": None}
        self.clock.run(self.mon, 10)
        self.assertEqual(self.gw.cycles, [])              # disabled -> no reset
        self.assertFalse(any("DOWN" in e["message"] and "Verizon" in e["message"] for e in self.events.recent()))

    def test_disabled_wan_still_shows_in_snapshot(self):
        self.settings.set(wan2_enabled=False)
        self.mon.check()
        wans = {w["label"]: w for w in self.mon.snapshot()["wans"]}
        self.assertFalse(wans["AT&T"]["enabled"])
        self.assertTrue(wans["Verizon"]["enabled"])

    def test_reenabling_a_wan_resumes_action(self):
        self.settings.set(wan1_enabled=False)
        self.gw.wan["wan1"] = {"up": False, "ip": None}
        self.clock.run(self.mon, 10)
        self.assertEqual(self.gw.cycles, [])
        self.settings.set(wan1_enabled=True)
        self.clock.run(self.mon, 6)                       # down timer now runs
        self.assertEqual(self.gw.cycles, [8])

    def test_snapshot_shows_box_reboot_watching(self):
        self.settings.set(box_reboot=True, box_reboot_wan="wan2", box_reboot_hour=4, box_reboot_latency_ms=90)
        mon, _ = self._reboot_mon(avg=120)
        st = mon.snapshot()["box_reboot"]
        self.assertEqual((st["on"], st["wan"], st["hour"], st["threshold"], st["recent_ms"], st["can"]),
                         (True, "wan2", 4, 90, 120, True))

    def test_snapshot_box_reboot_off_by_default(self):
        self.assertEqual(self.mon.snapshot()["box_reboot"], {"on": False})

    def test_automation_off_only_watches(self):
        self.settings.set(enabled=False)
        self.gw.wan["wan1"] = {"up": False, "ip": None}
        self.clock.run(self.mon, 10)
        self.assertEqual(self.gw.cycles, [])


class FakeDslCapture:
    """Saves AT&T broadband pages: each call saves the next page from `states` ((s1, s2) or (s1, s2, page
    keyword arguments)), one save per call. With `boot` set, a System Information page gives the box's uptime."""
    def __init__(self, clock, states=(), boot=None):
        from test_pages import dsl_page
        self.clock, self.page, self.saved, self.queue, self.boot = clock, dsl_page, {}, list(states), boot

    def add(self, s1, s2, ts, page=None):
        name = f"{int(ts):010d}-wan2"
        up = int(ts - self.boot) if self.boot is not None else None
        info = (f"<table><tr><th>Time Since Last Reboot</th><td>{up // 86400:02d}:{up // 3600 % 24:02d}:"
                f"{up // 60 % 60:02d}:{up % 60:02d}</td></tr></table>") if up is not None else ""
        self.saved[name] = (self.page(s1, s2, **(page or {})), ts, info)
        return name

    def has(self, key):
        return key == "wan2"

    def __call__(self, key, reason="", extra=None):
        s1, s2, *page = self.queue.pop(0)
        return self.add(s1, s2, self.clock(), *page), 3, []

    def list(self, key=None, day=None):
        return sorted(self.saved, reverse=True) if key in (None, "wan2") else []

    def pages(self, name):
        return [("home.ha", None), ("broadbandstatistics.ha", None), ("sysinfo.ha", None)]

    def raw(self, name, page):
        return {"broadbandstatistics.ha": self.saved[name][0], "sysinfo.ha": self.saved[name][2]}.get(page, "")

    def meta(self, name):
        return {"time": self.saved[name][1]}


class DslWatchTests(unittest.TestCase):
    setUp = MonitorTests.setUp

    def dsl_msgs(self):
        return [e["message"] for e in self.events.recent() if "bonded DSL" in e["message"]]

    def save(self, cap, minutes=0):
        self.clock.t += minutes * 60
        self.mon.save_pages(self.mon._wan("wan2"), "(test)", quiet=True)

    def test_pair_down_is_shown_then_logged_once_confirmed_then_recovery(self):
        cap = FakeDslCapture(self.clock, [("Down", "Up"), ("Down", "Up"), ("Down", "Up"), ("Up", "Up")])
        self.mon.capture = cap
        self.save(cap)
        d = self.mon._wan("wan2").dsl
        self.assertEqual((d["down"], d["confirmed"]), (["Line 1"], False))   # on the card, not logged yet
        self.assertEqual(self.dsl_msgs(), [])
        self.save(cap, 60)
        self.assertEqual(len(self.dsl_msgs()), 1)
        self.assertIn("Line 1 is down since", self.dsl_msgs()[0])
        self.assertIn("Line 2 only (60.0/11.1 Mbps)", self.dsl_msgs()[0])
        self.save(cap, 60)
        self.assertEqual(len(self.dsl_msgs()), 1)                          # not repeated
        self.save(cap, 60)
        self.assertIn("back to normal", self.dsl_msgs()[0])
        self.assertEqual(self.mon._wan("wan2").dsl["down"], [])
        snap = [w for w in self.mon.snapshot()["wans"] if w["key"] == "wan2"][0]
        self.assertEqual(snap["dsl"]["age"], 0)

    def test_retrain_blip_and_whole_line_down_are_not_logged(self):
        cap = FakeDslCapture(self.clock, [("Down", "Up"), ("Down", "TRAINING"), ("Up", "Up")])
        self.mon.capture = cap
        self.save(cap)
        self.save(cap, 1)                       # both down: the WAN's own problem, keeps "since"
        self.assertEqual(self.mon._wan("wan2").dsl["down"], ["Line 1"])
        self.save(cap, 1)
        self.assertEqual(self.dsl_msgs(), [])

    def test_start_reads_saved_history(self):
        cap = FakeDslCapture(self.clock)
        t0 = self.clock.t
        cap.add("Up", "Up", t0 - 7200)
        cap.add("Down", "Up", t0 - 3600)
        cap.add("Down", "TRAINING", t0 - 1800)  # ignored
        cap.add("Down", "Up", t0)
        self.mon.capture = cap
        self.mon.load_dsl()
        d = self.mon._wan("wan2").dsl
        self.assertEqual((d["down"], d["since"], d["confirmed"]), (["Line 1"], t0 - 3600, True))
        self.assertEqual(len(self.dsl_msgs()), 1)
        self.assertIsNone(self.mon._wan("wan1").dsl)


class DslAlertTests(unittest.TestCase):
    setUp = MonitorTests.setUp

    def card(self):
        return [w for w in self.mon.snapshot()["wans"] if w["key"] == "wan2"][0]["dsl"]

    def test_attainable_drop_is_on_the_card_and_logged_then_clears(self):
        boot = self.clock.t - 600
        ok = ("Up", "Up", {"ds": (30000, 30000), "dmax": (67000, 65000), "umax": (12400, 12100)})
        low = ("Up", "Up", {"ds": (30000, 30000), "dmax": (55000, 65000), "umax": (12400, 12100)})
        cap = FakeDslCapture(self.clock, [ok, low, low, ok], boot=boot)
        self.mon.capture = cap
        wan = self.mon._wan("wan2")
        self.mon.save_pages(wan, "(test)", quiet=True)
        self.assertEqual(self.card()["alerts"], {})
        for _ in range(2):
            self.clock.t += 3600
            self.mon.save_pages(wan, "(test)", quiet=True)
        self.assertEqual(list(self.card()["alerts"]), ["att_Line 1_ds_max"])
        msgs = [e["message"] for e in self.events.recent() if "max attainable" in e["message"]]
        self.assertEqual(len(msgs), 1)                                    # logged once, not every save
        self.assertIn("AT&T DSL Line 1 max attainable downstream down 18% since the reboot", msgs[0])
        self.clock.t += 3600
        self.mon.save_pages(wan, "(test)", quiet=True)
        self.assertEqual(self.card()["alerts"], {})
        self.assertTrue(self.events.recent()[0]["message"].startswith("Back to normal: AT&T DSL Line 1"))

    def test_low_attainable_after_a_reboot(self):
        cap = FakeDslCapture(self.clock, [("Up", "Up", {"dmax": (58000, 65000), "umax": (12400, 12100)})],
                             boot=self.clock.t - 300)
        self.mon.capture = cap
        self.mon.save_pages(self.mon._wan("wan2"), "(test)", quiet=True)
        self.assertIn("floor_Line 1", self.card()["alerts"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
