"""Speed tests: the probe side (request handling, measuring) and the watchdog side (schedule, results, line
details). Run: python tests/test_speedtest.py"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from test_monitor import Clock, FakeBouncer, FakeGateway, CFG  # noqa: E402  (also stubs requests/urllib3)

import config  # noqa: E402
import probe  # noqa: E402
import speedtest  # noqa: E402
from monitor import Monitor  # noqa: E402
from store import Events, Probes, Settings  # noqa: E402

CELL = {"network": {"type": "5G-NSA", "bandwidth_details": "B66: 20 MHz + N77: 100 MHz", "total_bandwidth_mhz": 120},
        "device": {"temperature": 48}, "lte": {"rsrp": -88}, "nr": {"rsrp": -95, "sinr": 14}}


class ParseTests(unittest.TestCase):
    def test_times(self):
        self.assertEqual(speedtest.parse_times("19:00, 1:30 04:30,01:30"), [(1, 30), (4, 30), (19, 0)])
        self.assertEqual(speedtest.parse_times(""), [])
        with self.assertRaises(ValueError):
            speedtest.parse_times("25:00")

    def test_settings_validate_times(self):
        ok, err = config.validate({"speedtest_times": "19:00, 1:30,4:30"})
        self.assertEqual((ok["speedtest_times"], err), ("01:30, 04:30, 19:00", []))
        _, err = config.validate({"speedtest_times": "1:30, noon"})
        self.assertIn("noon", err[0])
        self.assertEqual(config.initial_values()["speedtest_times"], "01:30, 04:30, 19:00")

    def test_doh(self):
        body = json.dumps({"Status": 0, "Answer": [{"type": 5, "data": "x.example."},
                                                   {"type": 1, "data": "162.159.140.220"}]})
        self.assertEqual(probe.parse_doh(body), ["162.159.140.220"])
        self.assertEqual(probe.parse_doh("nope"), [])
        self.assertEqual(probe.parse_doh("[1]"), [])

    def test_link_text(self):
        self.assertEqual(speedtest.link_text(speedtest.cell_link(CELL)),
                         "5G-NSA · B66: 20 MHz + N77: 100 MHz · 120 MHz total")
        dsl = {"ts": 1000, "lines": [{"line": "Line 1", "state": "Up", "down": 50000, "up": 10000},
                                     {"line": "Line 2", "state": "Down", "down": 0, "up": 0}]}
        link = speedtest.dsl_link(dsl, 1000 + 600)
        self.assertEqual((link["sync_down"], link["age_min"]), (50000, 10))
        self.assertEqual(speedtest.link_text(link), "DSL sync 50.0/10.0 Mbps, 1 of 2 pairs up")


class FakeResp:
    def __init__(self, status, n):
        self.status, self.left = status, n

    def getheader(self, name):
        return "LAX" if name == "colo" else None

    def read(self, n=-1):
        time.sleep(0.001)
        k = self.left if n < 0 else min(n, self.left)
        self.left -= k
        return b"\0" * k


class FakeConn:
    """Stands in for PinnedHTTPS: downloads hand out bytes, uploads consume the body."""
    status = 200

    def __init__(self, host, ip):
        self.resp = None

    def request(self, method, path, body=None, headers=None):
        if body is not None:
            for _ in body:
                time.sleep(0.001)
        self.resp = FakeResp(self.status, probe.SPEED_DOWN_BYTES if method == "GET" else 2)

    def getresponse(self):
        return self.resp

    def close(self):
        pass


class TransferTests(unittest.TestCase):
    def test_download_and_upload_count_bytes(self):
        for kind in ("down", "up"):
            r = probe.transfer(kind, "192.0.2.1", 0.5, streams=2, warmup=0.2, conn_cls=FakeConn)
            self.assertGreater(r["mbps"], 0, kind)
            self.assertGreater(r["mb"], 0, kind)
            self.assertEqual(r["errors"], [], kind)
            self.assertEqual(r["colo"], "LAX" if kind == "down" else None)

    def test_http_error_is_reported(self):
        class Refused(FakeConn):
            status = 403
        r = probe.transfer("down", "192.0.2.1", 0.3, streams=2, warmup=0.1, conn_cls=Refused)
        self.assertIsNone(r["mbps"])
        self.assertEqual(r["errors"], ["HTTP 403"])


class ProbeRequestTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.old = probe.SPEED_REQ, probe.SPEED_FILE, dict(probe._speed), probe.run_speedtest
        probe.SPEED_REQ = os.path.join(self.dir, "wan1.speedreq.json")
        probe.SPEED_FILE = os.path.join(self.dir, "wan1.speed.json")
        probe._speed.update(thread=None, done_id=None)
        self.ran = []
        probe.run_speedtest = lambda req: self.ran.append(req)

    def tearDown(self):
        probe.SPEED_REQ, probe.SPEED_FILE, speed, probe.run_speedtest = self.old
        probe._speed.clear()
        probe._speed.update(speed)

    def req(self, ident, seconds=10):
        with open(probe.SPEED_REQ, "w") as f:
            json.dump({"id": ident, "seconds": seconds}, f)

    def test_runs_each_request_once(self):
        self.assertFalse(probe.maybe_speedtest())            # no request
        now = time.time()
        self.req(now, 99)
        self.assertTrue(probe.maybe_speedtest(now))
        probe._speed["thread"].join(2)
        self.assertEqual(self.ran, [{"id": now, "seconds": 30}])     # clamped to 30 s
        self.assertFalse(probe.maybe_speedtest(now))         # same request: not again

    def test_old_request_and_restart(self):
        now = time.time()
        self.req(now - 3600)
        self.assertFalse(probe.maybe_speedtest(now))         # too old (found after a restart)
        with open(probe.SPEED_FILE, "w") as f:               # already done before the probe restarted
            json.dump({"id": now, "state": "done"}, f)
        self.req(now)
        probe._speed.update(done_id=None)
        self.assertFalse(probe.maybe_speedtest(now))
        self.assertEqual(self.ran, [])


class FakeModem:
    def __init__(self):
        self.status = {}
        self.samples = 0

    def qm_wans(self):
        return ["wan1"]

    def sample(self, wan):
        self.samples += 1
        self.status[wan] = {"ts": time.time(), "data": CELL}


class TesterTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.probes = os.path.join(d, "probes")
        os.mkdir(self.probes)
        self.events = Events(os.path.join(d, "events.db"))
        self.settings = Settings(os.path.join(d, "settings.json"), config.initial_values())
        self.gw = FakeGateway()
        self.clock = Clock()
        self.clock.t = time.time()
        self.mon = Monitor(CFG, self.gw, self.events, self.settings, clock=self.clock, bouncer=FakeBouncer(),
                           probe_dir=self.probes, probe_history=Probes(os.path.join(d, "probes.db")))
        self.probe_ok()
        self.mon.check()
        self.store = speedtest.SpeedStore(os.path.join(d, "speed.db"))
        self.modem = FakeModem()
        self.results = {}          # key -> what the fake probe answers
        self.t = speedtest.SpeedTester(self.store, self.events, self.settings, self.mon, modem=self.modem,
                                       probe_dir=self.probes, clock=self.clock, sleep=self.fake_sleep)

    def probe_ok(self):
        """Both WANs' internet probes answering, out through their own WAN."""
        for key in ("wan1", "wan2"):
            res = {"wan": key, "ts": self.clock.t, "ip": "10.0.0.5", "interval": 30, "state": "ok", "detail": "",
                   "targets": [{"target": "1.1.1.1", "sent": 3, "recv": 3, "avg_ms": 30.0}],
                   "trace": {"ok": True, "exit_ip": self.gw.wan[key]["ip"]}}
            with open(os.path.join(self.probes, f"{key}.json"), "w") as f:
                json.dump(res, f)

    def fake_sleep(self, s):
        """Time passes; the 'probe' answers the request it finds: running (download) first, then done."""
        self.clock.t += s
        for key in ("wan1", "wan2", "vpn"):
            path = os.path.join(self.probes, f"{key}.speedreq.json")
            if not os.path.exists(path) or key not in self.results:
                continue
            with open(path) as f:
                req = json.load(f)
            out = os.path.join(self.probes, f"{key}.speed.json")
            cur = json.load(open(out)) if os.path.exists(out) else {}
            if cur.get("id") != req["id"]:
                res = {"id": req["id"], "state": "running", "phase": "down", "phase_started": self.clock.t}
            elif cur.get("state") == "running" and self.clock.t - cur["phase_started"] >= 6:
                res = dict(self.results[key], id=req["id"])
            else:
                continue
            with open(out, "w") as f:
                json.dump(res, f)

    def done(self, down, up):
        return {"state": "done", "down_mbps": down, "up_mbps": up, "idle_ms": 30.2, "down_ms": 80.0,
                "up_ms": 120.0, "down_mb": 600.0, "up_mb": 50.0, "colo": "LAX", "exit_ip": "203.0.113.10"}

    def test_run_records_line_details(self):
        self.results = {"wan1": self.done(412.3, 38.1), "wan2": self.done(88.0, 17.5)}
        self.mon._wan("wan2").dsl = {"ts": self.clock.t - 900, "lines": [
            {"line": "Line 1", "state": "Up", "down": 50000, "up": 10000},
            {"line": "Line 2", "state": "Up", "down": 50000, "up": 10000}]}
        ok, msg = self.t.start("test")
        self.assertTrue(ok, msg)
        self.t.thread.join(10)
        rows = {r["wan"]: r for r in self.store.recent(0)}
        self.assertEqual(rows["wan1"]["down_mbps"], 412.3)
        self.assertEqual(rows["wan1"]["link"]["carriers"], "B66: 20 MHz + N77: 100 MHz")
        self.assertEqual(rows["wan1"]["data_mb"], 650.0)
        self.assertEqual(self.modem.samples, 1)              # read once, during the download
        self.assertEqual(rows["wan2"]["link"]["sync_down"], 100000)
        self.assertEqual(rows["wan2"]["link"]["age_min"], 15)
        log = [e["message"] for e in self.events.recent()]
        self.assertTrue(any("Speed test Verizon: 412 down / 38 up Mbps" in m and "N77: 100 MHz" in m for m in log))
        g = speedtest.by_setup(self.store.recent(0))
        self.assertEqual((g[0]["carriers"], g[0]["tests"], g[0]["per_mhz"]), ("B66: 20 MHz + N77: 100 MHz", 1, 3.44))
        self.assertIsNone(self.t.snapshot())

    def test_down_wan_is_skipped_and_dead_probe_fails(self):
        self.gw.wan["wan2"] = {"up": False, "ip": ""}
        os.remove(os.path.join(self.probes, "wan2.json"))   # (a probe still getting out would vouch for it)
        os.remove(os.path.join(self.probes, "wan1.json"))   # and no wan1 probe container at all
        self.settings.set(probe_check=False)
        self.mon.check()
        ok, _ = self.t.start("test")                         # no probe answers for wan1 either
        self.assertTrue(ok)
        self.t.thread.join(10)
        rows = {r["wan"]: r for r in self.store.recent(0)}
        self.assertEqual((rows["wan2"]["state"], rows["wan2"]["error"]), ("skipped", "WAN is down"))
        self.assertEqual(rows["wan1"]["state"], "failed")
        self.assertIn("didn't start", rows["wan1"]["error"])

    def test_disabled_wan_not_tested_and_one_at_a_time(self):
        self.settings.set(wan2_enabled=False)
        self.assertEqual([k for k, _ in self.t.targets()], ["wan1"])
        self.t.sleep = lambda s: time.sleep(0.05)            # keep the first run going
        self.assertTrue(self.t.start("a")[0])
        self.assertEqual(self.t.start("b"), (False, "a speed test is already running"))

    def test_schedule(self):
        self.settings.set(speedtest_times="01:30, 19:00")
        started = []
        self.t.start = lambda trigger, only=None: (started.append(trigger), (True, ""))[1]
        self.assertFalse(self.t.tick(datetime(2026, 11, 12, 1, 29)))
        self.assertTrue(self.t.tick(datetime(2026, 11, 12, 1, 30, 5)))
        self.assertFalse(self.t.tick(datetime(2026, 11, 12, 1, 30, 40)))     # same slot: once
        self.assertTrue(self.t.tick(datetime(2026, 11, 13, 1, 30)))
        self.assertEqual(started, ["scheduled 01:30", "scheduled 01:30"])
        self.assertEqual(self.t.next_run(datetime(2026, 11, 12, 12, 0)), "19:00")
        self.assertEqual(self.t.next_run(datetime(2026, 11, 12, 20, 0)), "tomorrow 01:30")
        self.settings.set(speedtest_enabled=False)
        self.assertFalse(self.t.tick(datetime(2026, 11, 14, 1, 30)))
        self.assertIsNone(self.t.next_run(datetime(2026, 11, 12, 12, 0)))


class MonitorTests(unittest.TestCase):
    def test_saturated_probe_rounds_not_in_history(self):
        d = tempfile.mkdtemp()
        probes = os.path.join(d, "probes")
        os.mkdir(probes)
        hist = Probes(os.path.join(d, "probes.db"))
        clock = Clock()
        clock.t = time.time()
        mon = Monitor(CFG, FakeGateway(), Events(os.path.join(d, "e.db")),
                      Settings(os.path.join(d, "s.json"), config.initial_values()), clock=clock,
                      bouncer=FakeBouncer(), probe_dir=probes, probe_history=hist)
        for i, busy in enumerate((False, True)):
            clock.t += 30
            res = {"wan": "wan1", "ts": clock.t, "ip": "10.0.0.5", "interval": 30, "state": "ok", "detail": "",
                   "targets": [{"target": "1.1.1.1", "sent": 3, "recv": 3, "avg_ms": 300.0 if busy else 30.0}],
                   "trace": {"ok": True, "exit_ip": "203.0.113.10"}}
            if busy:
                res["speedtest"] = True
            with open(os.path.join(probes, "wan1.json"), "w") as f:
                json.dump(res, f)
            mon.check()
        self.assertEqual(hist.recent("wan1", 0), (1, 30.0, 0.0))     # the 300 ms round under load left out


if __name__ == "__main__":
    unittest.main()
