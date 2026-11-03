"""UniFi client: the power-cycle fallback must never send a second power cycle. Run: python tests/test_unifi.py"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from unifi import UniFi, UniFiError  # noqa: E402


class FakeUniFi(UniFi):
    def __init__(self, integration_error=None, lookup_error=False):
        super().__init__("https://gw.example", "key")
        self.integration_error, self.lookup_error, self.posts = integration_error, lookup_error, []

    def site_id(self):
        if self.lookup_error:
            raise UniFiError("GET sites -> HTTP 404", status=404)
        return "site"

    def device_id(self, mac):
        return "dev"

    def _request(self, method, path, **kwargs):
        self.posts.append("integration" if "integration" in path else "classic")
        if "integration" in path and self.integration_error:
            raise self.integration_error
        return {}


class PowerCycleTests(unittest.TestCase):
    def test_integration_api_ok(self):
        u = FakeUniFi()
        self.assertEqual(u.power_cycle("aa:bb:cc:00:00:01", 1), "Integration API")
        self.assertEqual(u.posts, ["integration"])

    def test_timeout_is_not_retried_with_the_classic_api(self):
        u = FakeUniFi(integration_error=UniFiError("POST ...: ReadTimeout"))
        with self.assertRaises(UniFiError) as cm:
            u.power_cycle("aa:bb:cc:00:00:01", 1)
        self.assertIn("may have happened", str(cm.exception))
        self.assertEqual(u.posts, ["integration"])                 # no second power cycle

    def test_server_error_is_not_retried(self):
        u = FakeUniFi(integration_error=UniFiError("HTTP 502", status=502))
        with self.assertRaises(UniFiError):
            u.power_cycle("aa:bb:cc:00:00:01", 1)
        self.assertEqual(u.posts, ["integration"])

    def test_rejected_request_falls_back(self):
        for status in (400, 401, 403, 404, 405, 501):
            u = FakeUniFi(integration_error=UniFiError(f"HTTP {status}", status=status))
            self.assertEqual(u.power_cycle("aa:bb:cc:00:00:01", 1), "classic API")
            self.assertEqual(u.posts, ["integration", "classic"])

    def test_lookup_failure_falls_back_without_integration_post(self):
        u = FakeUniFi(lookup_error=True)
        self.assertEqual(u.power_cycle("aa:bb:cc:00:00:01", 1), "classic API")
        self.assertEqual(u.posts, ["classic"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
