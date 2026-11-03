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

    def test_login_redirect_stays_on_site(self):
        r = self.login("/%5Cevil.example")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["Location"], "/")

    def test_bad_days_value_does_not_crash(self):
        self.login("/")
        r = self.c.get("/captures/combined?days=abc")
        self.assertIn(r.status_code, (200, 302))


if __name__ == "__main__":
    unittest.main(verbosity=2)
