"""Web app checks (login redirect, bad query values). Needs flask/waitress/requests. Run: python tests/test_main.py"""
import os
import sys
import tempfile
import unittest

os.environ.update(UNIFI_API_KEY="test", ADMIN_PASSWORD="test-pw", DATA_DIR=tempfile.mkdtemp(),
                  UNIFI_URL="https://127.0.0.1:9")      # nothing listens: the app never reaches a real gateway
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import main  # noqa: E402


class WebTests(unittest.TestCase):
    def setUp(self):
        self.c = main.app.test_client()

    def login(self, nxt):
        return self.c.post(f"/login?next={nxt}", data={"password": "test-pw"})

    def test_safe_next(self):
        for bad in ("//evil.example", "/\\evil.example", "/%5Cevil.example", "https://evil.example", "evil", "/\x09x"):
            from urllib.parse import unquote
            self.assertEqual(main.safe_next(unquote(bad)), "/", bad)
        self.assertEqual(main.safe_next("/captures?wan=wan1"), "/captures?wan=wan1")

    def test_internet_page_and_dashboard_render(self):
        self.login("/")
        main.probe_history.add(main.time.time() - 60, "wan2", "ok", 6, 5, 41.5, "198.51.100.20")
        main.probe_history.add(main.time.time() - 30, "wan2", "fail", 6, 0, None, None)
        for url in ("/internet", "/internet?days=7", "/internet?days=x", "/"):
            r = self.c.get(url)
            self.assertEqual(r.status_code, 200, url)
        body = self.c.get("/internet").get_data(as_text=True)
        self.assertIn("58.33%", body)                     # 7 of 12 pings lost over the period
        self.assertIn('data-refresh="30"', body)
        home = self.c.get("/").get_data(as_text=True)
        self.assertIn('data-refresh="10"', home)
        self.assertIn('id="last-check"', home)
        self.assertIn("<body>", self.c.get("/settings/all").get_data(as_text=True))   # forms: no auto refresh

    def test_speed_page(self):
        self.login("/")
        now = main.time.time()
        main.speed_store.add({"ts": now - 60, "wan": "wan1", "label": "Verizon", "trigger": "scheduled 19:00",
                              "state": "done", "down_mbps": 412.3, "up_mbps": 38.1, "idle_ms": 30, "down_ms": 80,
                              "up_ms": 120, "data_mb": 650, "colo": "LAX",
                              "link": {"kind": "cell", "type": "5G-NSA", "carriers": "B66: 20 MHz + N77: 100 MHz",
                                       "total_mhz": 120, "nr_sinr": 14}})
        main.speed_store.add({"ts": now - 30, "wan": "wan2", "label": "AT&T", "trigger": "scheduled 19:00",
                              "state": "done", "down_mbps": 88.0, "up_mbps": 17.5, "data_mb": 120,
                              "link": {"kind": "dsl", "sync_down": 100000, "sync_up": 20000, "pairs_up": 2,
                                       "pairs": 2, "age_min": 15}})
        main.speed_store.add({"ts": now - 20, "wan": "wan2", "label": "AT&T", "trigger": "on request",
                              "state": "skipped", "error": "WAN is down"})
        self.assertEqual(sorted(self.c.get("/api/last-check").get_json()), ["interval", "last_check"])
        self.assertIn("/api/last-check", self.c.get("/settings/all").get_data(as_text=True))   # every page
        for url in ("/speed", "/speed?days=365", "/speed?days=x"):
            self.assertEqual(self.c.get(url).status_code, 200, url)
        body = self.c.get("/speed").get_data(as_text=True)
        for text in ("412.3", "B66: 20 MHz + N77: 100 MHz", "3.44", "88%", "DSL sync 100.0/20.0 Mbps", "WAN is down",
                     "By carrier setup", "Speed tests"):
            self.assertIn(text, body)
        self.assertIn("Speed test", self.c.get("/settings/all").get_data(as_text=True))
        with self.c.session_transaction() as sess:
            token = sess.get("csrf")
        r = self.c.post("/speed/run", data={"csrf": token})
        self.assertEqual(r.status_code, 302)

    def test_login_redirect_stays_on_site(self):
        r = self.login("/%5Cevil.example")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["Location"], "/")

    def test_modem_pages_render(self):
        self.login("/")
        now = main.time.time()
        main.modem_store.add_metrics("wan1", now - 600, {"temp": 61, "cpu_temp": 66, "poe_w": 2.8})
        main.modem_store.add_signal("wan1", [{"ts": now - 60, "lte_rsrp": [-71, -72, -140, -140],
                                              "nr_sinr": [15, 22, -20, -20]}])
        for url in ("/modem", "/modem?tab=health&range=7d", "/modem?range=month", "/modem?range=bad", "/"):
            self.assertEqual(self.c.get(url).status_code, 200, url)
        body = self.c.get("/modem").get_data(as_text=True)
        self.assertIn("Signal history", body)
        self.assertIn("No signal", body)                  # -140 is shown as no signal, not "Poor"
        for k in ("auto", "light", "dark"):                 # theme switch in the sidebar
            self.assertIn(f'data-theme-pick="{k}"', body)
        self.assertIn('data-refresh="10"', body)           # checks for new data every 10 s ...
        self.assertIn('data-refresh-when="/api/modem/wan1/stamp?tab=signal"', body)
        before = self.c.get("/api/modem/wan1/stamp?tab=health").get_json()["stamp"]
        main.modem_store.add_metrics("wan1", now - 5, {"poe_w": 2.9})       # ... and redraws when the stamp moves
        self.assertGreater(self.c.get("/api/modem/wan1/stamp?tab=health").get_json()["stamp"], before)
        sig = self.c.get("/api/modem/wan1/stamp").get_json()["stamp"]
        self.assertEqual(sig, int(now - 60))                # signal tab: antenna data, not PoE
        self.assertIn(">Week</a>", body)
        self.assertIn(">Month</a>", body)
        self.assertIn("last month", self.c.get("/modem?tab=health&range=month").get_data(as_text=True))
        d = self.c.get("/api/modem/wan1/series?names=temp,poe_w,bad-name!&range=24h").get_json()
        self.assertEqual(sorted(d), ["poe_w", "temp"])
        self.assertEqual(d["temp"][0][3], 61)
        self.assertEqual(self.c.get("/api/modem/wan1/signal").get_json()["lte_rsrp"][0][1], -71)
        self.assertEqual(self.c.get("/api/modem/wan9/signal").status_code, 404)
        self.assertIn("66", self.c.get("/").get_data(as_text=True))    # CPU 66 °C on the Verizon card
        self.assertEqual(self.c.get("/static/chart.js").status_code, 200)

    def test_bad_days_value_does_not_crash(self):
        self.login("/")
        r = self.c.get("/captures/combined?days=abc")
        self.assertIn(r.status_code, (200, 302))


if __name__ == "__main__":
    unittest.main(verbosity=2)
