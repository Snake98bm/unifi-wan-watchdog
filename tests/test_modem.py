"""Modem health: signal parsing, the store, heat / slow-down / PoE checks, saved-page numbers.
Run: python tests/test_modem.py"""
import json
import os
import sys
import tempfile
import time
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
for name in ("requests", "urllib3"):
    if name not in sys.modules:
        stub = types.ModuleType(name)
        stub.Session = object
        stub.RequestException = Exception
        stub.exceptions = types.SimpleNamespace(InsecureRequestWarning=Warning)
        stub.disable_warnings = lambda *a: None
        sys.modules[name] = stub

import modem  # noqa: E402
from atparse import at_html, fix_old_qcainfo, text_html  # noqa: E402
from store import Events  # noqa: E402

# what QManager reported on 2026-11-12 08:00 (trimmed)
SPA = {"lte_rsrp": [-71, -72, -140, -140], "lte_rsrq": [-8, -8, None, None], "lte_sinr": [13, 10, None, None],
       "nr_rsrp": [-78, -74, -67, -69], "nr_rsrq": [-10, -10, -10, -10], "nr_sinr": [15, 22, -20, -20]}
STATUS = {"network": {"type": "5G-NSA", "bandwidth_details": "B66: 20 MHz + B5: 10 MHz + N77: 60 MHz",
                      "total_bandwidth_mhz": 90},
          "lte": {"rsrp": -72}, "nr": {"rsrp": -67, "sinr": 22}, "signal_per_antenna": SPA,
          "device": {"temperature": 51, "cpu_usage": 50, "poller_tier": "idle"}}


class Settings(dict):
    def get(self, k, d=None):
        return dict.get(self, k, d)


class FakeCapture:
    def __init__(self, pages=None):
        self.urls_by_wan = {"wan1": [{"url": "https://192.0.2.1/cgi-bin/quecmanager/at_cmd/fetch_data.sh"}],
                            "wan2": [{"url": "http://192.0.2.254/cgi-bin/home.ha"}]}
        self.saved = pages or {}           # name -> {page: html}
        self.fetch = None

    def qm_base(self, wan):
        return "https://192.0.2.1" if wan == "wan1" else None

    def qm_fetch(self, wan, paths):
        if isinstance(self.fetch, Exception):
            raise self.fetch
        return self.fetch

    def list(self, wan):
        return sorted((n for n in self.saved if n.endswith(wan)), reverse=True)

    def pages(self, name):
        return [(k, k) for k in self.saved.get(name, {})]

    def raw(self, name, page):
        return self.saved[name][page]

    def meta(self, name):
        return {}


class SignalTests(unittest.TestCase):
    def test_sentinels_are_no_signal_not_poor(self):
        a = modem.antennas(SPA)
        self.assertEqual((a["lte"]["chains"], a["nr"]["chains"]), (2, 4))
        mimo3 = a["lte"]["ports"][2]
        self.assertFalse(mimo3["live"])
        self.assertEqual(mimo3["verdict"], "none")
        self.assertEqual([r["value"] for r in mimo3["rows"]], [None, None, None])
        nr3 = a["nr"]["ports"][2]
        self.assertEqual(nr3["rows"][2]["value"], None)          # SINR -20 = idle chain
        self.assertEqual(nr3["verdict"], "excellent")            # RSRP -67 is real
        self.assertEqual(a["nr"]["ports"][0]["rows"][2]["label"], "SNR")
        self.assertEqual(a["lte"]["best"], {"rsrp": -71, "rsrq": -8, "sinr": 13})

    def test_quality_bands(self):
        self.assertEqual([modem.quality(v, "rsrp") for v in (-80, -95, -105, -115, -125, None)],
                         ["excellent", "good", "fair", "poor", "bad", "none"])
        self.assertEqual(modem.quality(-19, "rsrq"), "bad")      # -19 dB RSRQ is a real reading
        self.assertIsNotNone(modem.norm(-20, "rsrq"))
        self.assertIsNone(modem.norm(-32768, "rsrq"))

    def test_flattened_saved_values(self):
        a = modem.antennas({"lte_rsrp": "-71, -72, -140, -140", "nr_sinr": "15, 22, -20, -20"})
        self.assertEqual(a["lte"]["chains"], 2)
        self.assertEqual(a["nr"]["chains"], 2)

    def test_carriers(self):
        self.assertEqual(modem.carrier_count("B66: 20 MHz + B5: 10 MHz + N77: 60 MHz"), 3)
        self.assertEqual(modem.carrier_count(""), 0)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.store = modem.ModemStore(os.path.join(tempfile.mkdtemp(), "modem.db"))

    def test_signal_history_dedupes_and_series(self):
        now = int(time.time())
        hist = [dict(SPA, ts=now - 30 + i * 10) for i in range(3)]
        self.assertEqual(self.store.add_signal("wan1", hist), 3)
        self.assertEqual(self.store.add_signal("wan1", hist + [dict(SPA, ts=now + 10)]), 1)
        ser = self.store.signal_series("wan1", now - 3600, now + 60)
        self.assertEqual(ser["lte_rsrp"][0][1], -71)
        self.assertEqual(ser["nr_chains"][0][1], 4)
        latest = self.store.latest_signal("wan1")
        self.assertEqual(latest["lte_rsrp"], [-71, -72, None, None])

    def test_metrics_series_bucketed(self):
        now = time.time()
        for i in range(10):
            self.store.add_metrics("wan1", now - 600 + i * 60, {"temp": 50 + i, "poe_w": None})
        ser = self.store.series("wan1", ["temp", "poe_w"], now - 3600, now, points=4)
        self.assertEqual(ser["poe_w"], [])
        self.assertEqual(max(p[3] for p in ser["temp"]), 59)
        self.assertEqual(self.store.latest("wan1", "temp"), (now - 600 + 540, 59.0))


class SavedPagesTests(unittest.TestCase):
    def test_capture_metrics(self):
        pages = {
            "AT_QTEMP": at_html("AT+QTEMP", ['+QTEMP:"cpuss-0-usr","74"', '+QTEMP:"modem-ambient-usr","61"',
                                             '+QTEMP:"modem-lte-sub6-pa1","48"', '+QTEMP:"modem-mmw0","-273"']),
            "AT_CBC": at_html("AT+CBC", ["+CBC: 0,38,3702"]),
            "fetch_data.sh": text_html("fetch_data.sh", json.dumps(STATUS)),
            "modem_sensors": modem.sensors_html(modem.parse_sensors(
                "in_temp_pmx65_mdm_case_therm_input:50600\nin_temp_qtm_skin_therm_input:-40960\n"
                "in_voltage_vph_pwr_input:3703155\n")),
        }
        m = modem.capture_metrics(pages)
        self.assertEqual((m["cpu_temp"], m["ambient_temp"], m["pa_temp"], m["supply_mv"]), (74, 61, 48, 3702))
        self.assertEqual((m["temp"], m["carriers"], m["lte_chains"], m["nr_chains"]), (51, 3, 2, 4))
        self.assertEqual((m["case_temp"], m["vph_mv"]), (50.6, 3703.2))

    def test_old_cbc_label(self):
        old = '<table class="grid"><tr><th>Field</th><th>Value</th></tr><tr class="a"><td class="heading">CBC 3</td><td>3699</td></tr></table>'
        self.assertEqual(modem.capture_metrics({"AT_CBC": old})["supply_mv"], 3699)

    def test_old_qcainfo_relabelled(self):
        rows = [("PCC channel", "66536"), ("PCC bandwidth", "100"), ("PCC band", "LTE BAND 66"), ("PCC state", "1"),
                ("PCC PCI", "361"), ("PCC RSRP", "-71"), ("PCC RSRQ", "-9"), ("PCC RSSI", "-43"), ("PCC SINR", "12"),
                ("SCC 1 channel", "653952"), ("SCC 1 bandwidth", "8"), ("SCC 1 band", "NR5G BAND 77"),
                ("SCC 1 state", "362"), ("SCC 1 PCI", "-75"), ("SCC 1 RSRP", "-11"), ("SCC 1 RSRQ", "1450")]
        old = "<h2>AT+QCAINFO</h2><table class=\"grid\"><tr><th>Field</th><th>Value</th></tr>" + "".join(
            f'<tr class="a"><td class="heading">{k}</td><td>{v}</td></tr>' for k, v in rows) + "</table>"
        new = modem.table_values(fix_old_qcainfo(old))
        self.assertEqual((new["SCC 1 PCI"], new["SCC 1 RSRP"], new["SCC 1 RSRQ"], new["SCC 1 SINR"]),
                         ("362", "-75", "-11", "14.5"))
        self.assertEqual(new["PCC RSSI"], "-43")
        fixed = fix_old_qcainfo(old)
        self.assertEqual(fix_old_qcainfo(fixed), fixed)         # already fixed: left alone


class WatchTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.events = Events(os.path.join(d, "events.db"))
        self.store = modem.ModemStore(os.path.join(d, "modem.db"))
        self.settings = Settings(modem_temp_warn=70, modem_temp_alert=75, modem_hot_c=60, poe_step_w=0.5)
        self.cap = FakeCapture()
        self.now = time.time()
        self.watch = modem.ModemWatch(self.store, self.events, self.settings, capture=self.cap,
                                      clock=lambda: self.now)

    def msgs(self):
        return [e["message"] for e in reversed(self.events.recent(100))]

    def status(self, temp, lte=SPA["lte_rsrp"], details=STATUS["network"]["bandwidth_details"]):
        self.now += 1500
        data = json.loads(json.dumps(STATUS))
        data["device"]["temperature"] = temp
        data["signal_per_antenna"]["lte_rsrp"] = lte
        data["network"]["bandwidth_details"] = details
        self.watch.ingest_status("wan1", self.now, data)

    def test_qm_wans(self):
        self.assertEqual(self.watch.qm_wans(), ["wan1"])

    def test_heat_warn_alert_and_clear(self):
        self.status(65)
        self.status(71)
        self.status(76)
        self.status(73)
        self.status(66)
        m = self.msgs()
        self.assertTrue(any("Modem is hot: 71" in x for x in m))
        self.assertTrue(any("VERY HOT: 76" in x for x in m))
        self.assertTrue(any("cooled to 73" in x for x in m))
        self.assertTrue(any("back to normal: 66" in x for x in m))
        self.assertEqual(self.events.recent(100, level="error")[0]["message"][:22], "Modem is VERY HOT: 76 ")

    def test_small_dip_below_warn_does_not_clear(self):
        self.status(71)
        self.status(69)
        self.assertEqual(self.watch.heat["wan1"], "warn")

    def test_cpu_sensor_counts(self):
        self.store.add_metrics("wan1", self.now, {"cpu_temp": 74})
        self.status(55)
        self.assertEqual(self.watch.heat["wan1"], "warn")
        self.assertEqual(self.watch.temperature("wan1"), (74, "CPU"))

    def test_slow_down_needs_heat_and_two_readings(self):
        for _ in range(8):
            self.status(45)                                       # cool baseline: 2 LTE, 4 NR, 3 carriers
        self.status(50, lte=[-71, -140, -140, -140])              # lost a chain while cool: not heat
        self.status(50, lte=[-71, -140, -140, -140])
        self.assertFalse(any("slowing" in x for x in self.msgs()))
        self.status(64, details="B66: 20 MHz + N77: 60 MHz")
        self.assertFalse(any("slowing" in x for x in self.msgs()))
        self.status(65, details="B66: 20 MHz + N77: 60 MHz")
        self.assertTrue(any("slowing down from heat: 65 °C and carriers 2 (usually 3)" in x for x in self.msgs()))
        self.status(64)
        self.assertTrue(any("back to its usual" in x for x in self.msgs()))

    def test_poe_step_and_stop(self):
        t = self.now
        for i in range(70):                                       # 35 min at 2.8 W
            self.watch.poe_sample("wan1", "Verizon", 8, t + i * 30, {"power": 2.8, "voltage": 53.2, "current": 53})
        for i in range(70, 81):                                   # 5.5 min at 3.6 W
            self.watch.poe_sample("wan1", "Verizon", 8, t + i * 30, {"power": 3.6, "voltage": 53.2, "current": 68})
        steps = [x for x in self.msgs() if "PoE draw changed" in x]
        self.assertEqual(len(steps), 1)
        self.assertIn("2.8 W -> 3.6 W", steps[0])
        for i in range(81, 200):
            self.watch.poe_sample("wan1", "Verizon", 8, t + i * 30, {"power": 0.0, "voltage": 0, "current": 0})
        self.assertTrue(any("stopped drawing PoE on port 8" in x for x in self.msgs()))
        self.assertEqual(len(self.store.series("wan1", ["poe_ma"], t - 1, t + 7000)["poe_ma"]) > 0, True)

    def test_poe_steps_off(self):
        self.settings["poe_step_w"] = 0
        for i in range(100):
            self.watch.poe_sample("wan1", "Verizon", 8, self.now + i * 30, {"power": 2.8 if i < 80 else 4,
                                                                             "voltage": 53, "current": 50})
        self.assertFalse(any("PoE" in x for x in self.msgs()))

    def test_sampler_stores_history_and_status(self):
        now = int(self.now)
        self.cap.fetch = {"at_cmd/fetch_signal_history.sh": json.dumps([dict(SPA, ts=now - 20), dict(SPA, ts=now - 10)]),
                          "at_cmd/fetch_data.sh": json.dumps(STATUS)}
        self.assertEqual(self.watch.sample("wan1"), (2, None))
        self.assertEqual(self.watch.sample("wan1")[0], 0)        # same history again: nothing new
        snap = self.watch.snapshot("wan1")
        self.assertEqual((snap["temp"], snap["antennas"]["lte"]["chains"]), (51, 2))

    def test_sampler_error_logged_once(self):
        self.cap.fetch = PermissionError("login refused - check QUECTEL_PASSWORD")
        self.watch.sample("wan1")
        self.watch.sample("wan1")
        self.assertEqual(len([x for x in self.msgs() if "sampler couldn't" in x]), 1)
        self.cap.fetch = {"at_cmd/fetch_signal_history.sh": "[]", "at_cmd/fetch_data.sh": json.dumps(STATUS)}
        self.watch.sample("wan1")
        self.assertIn("sampler works again", self.msgs()[-1])

    def test_backfill_from_saves(self):
        self.cap.saved = {"20261111-120000-wan1": {"AT_QTEMP": at_html("AT+QTEMP", ['+QTEMP:"cpuss-0-usr","74"'])},
                          "20261111-130000-wan1": {"AT_CBC": at_html("AT+CBC", ["+CBC: 0,38,3702"]),
                                                   "fetch_data.sh": text_html("fetch_data.sh", json.dumps(STATUS))},
                          "20261111-120000-wan2": {"home.ha": "<p>AT&T</p>"}}
        self.assertEqual(self.watch.backfill(), 2)
        ts, v = self.store.latest("wan1", "cpu_temp")
        self.assertEqual(v, 74)
        self.assertEqual(time.strftime("%Y%m%d-%H%M%S", time.localtime(ts)), "20261111-120000")
        self.assertEqual(self.events.recent(10), [])             # history isn't re-alerted
        # the save's QManager status fills the antenna cards and adds one signal point
        self.assertEqual(self.store.latest_signal("wan1")["lte_rsrp"], [-71, -72, None, None])
        snap = self.watch.snapshot("wan1")
        self.assertEqual((snap["antennas"]["nr"]["chains"], snap["network"]["type"]), (4, "5G-NSA"))


class SensorTests(unittest.TestCase):
    def test_parse(self):
        v = modem.parse_sensors("in_temp_pmx65_pa_therm1_input:47100\nin_temp_qtm_skin_therm_input:-40960\n"
                                "junk\nin_voltage_vph_pwr_input:3703155\n")
        self.assertEqual(v, {"pmx65_pa_therm1": 47.1, "vph_pwr": 3703.2})


if __name__ == "__main__":
    unittest.main()
