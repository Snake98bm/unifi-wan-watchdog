"""Capture tests against a fake AT&T box (cookie, then nonce login form, md5(code + nonce)). Needs requests.
Run: python tests/test_capture.py"""
import hashlib
import http.server
import os
import secrets
import sys
import tempfile
import threading
import unittest
from urllib.parse import parse_qs

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from capture import Capturer  # noqa: E402

CODE = "test-code-123"
STATE = {"nonce": None, "sessions": set(), "authed": set(), "logins": 0}
LOGIN_NO_COOKIE = "<html><h1>Access Code Required</h1>Cookies must be enabled in your browser to log in.</html>"


class FakeBox(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _sid(self):
        cookie = self.headers.get("Cookie", "")
        return cookie.split("SessionID=", 1)[1].split(";")[0] if "SessionID=" in cookie else None

    def _send(self, body, sid=None):
        self.send_response(200)
        if sid:
            self.send_header("Set-Cookie", f"SessionID={sid}; path=/")
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self):
        sid = self._sid()
        if self.path.startswith("/cgi-bin/get_atcommand"):   # Quectel toolkit: HTTP Basic login
            import base64
            if self.headers.get("Authorization") != "Basic " + base64.b64encode(b"admin:modem-pw").decode():
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="Authorized users only"')
                self.end_headers()
                return
            STATE["at"].append(self.path)
            return self._send('AT+CSQ\r\n+CSQ: 22,99\r\n\r\nOK\r\n')
        if self.path.endswith("public.ha"):
            return self._send("<table><tr><td>Line State</td><td>Up</td></tr></table>")
        if not sid:
            sid = secrets.token_hex(4)
            STATE["sessions"].add(sid)
            return self._send(LOGIN_NO_COOKIE, sid)
        if sid in STATE["authed"]:
            return self._send("<table><tr><td>syslog</td><td>DSL line trained</td></tr></table>")
        STATE["nonce"] = secrets.token_hex(8)
        return self._send(f'<form method="post" action="/cgi-bin/login.ha">'
                          f'<input type="hidden" name="nonce" value="{STATE["nonce"]}" /></form>')

    def do_POST(self):
        STATE["logins"] += 1
        form = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
        ok = form.get("hashpassword") == [hashlib.md5((CODE + STATE["nonce"]).encode()).hexdigest()]
        if ok and set(form["password"][0]) == {"*"}:   # plain code must never be sent
            STATE["authed"].add(self._sid())
        self._send("ok")


class CaptureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeBox)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}/cgi-bin/"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        STATE.update(nonce=None, sessions=set(), authed=set(), logins=0, at=[])
        self.dir = tempfile.mkdtemp()

    def at_items(self):
        import config
        return config.capture_urls({"capture_wan1": "at:AT+CSQ\nat:AT+QENG=\"servingcell\"",
                                    "at_url_wan1": self.base + "get_atcommand?atcmd={AT}"})["wan1"]

    def test_quectel_at_commands_with_login(self):
        cap = Capturer(self.dir, {"wan1": self.at_items()}, auth_by_wan={"wan1": ("admin", "modem-pw")})
        name, saved, errors = cap("wan1")
        self.assertEqual((saved, errors), (2, []))
        self.assertIn("atcmd=AT%2BQENG%3D%22servingcell%22", STATE["at"][1])      # command URL-encoded
        pages_ = dict(Capturer(self.dir, {}).files(name))
        self.assertIn("RSSI (0-31) | 22", pages_["01-AT_CSQ"])

    def test_quectel_wrong_password_stops_after_first_try(self):
        cap = Capturer(self.dir, {"wan1": self.at_items()}, auth_by_wan={"wan1": ("admin", "wrong")})
        _, saved, errors = cap("wan1")
        self.assertEqual(saved, 0)
        self.assertEqual(len(errors), 1)
        self.assertIn("login refused", errors[0])

    def test_unreachable_modem_gives_up_fast_but_keeps_gateway_log(self):
        import time as _t
        dead = [{"url": "http://127.0.0.1:9/x", "name": f"p{i}", "at": None} for i in range(10)]   # port 9: refused
        start = _t.time()
        name, saved, errors = Capturer(self.dir, {"wan1": dead})("wan1", extra=[("gateway_wan_log", "<table></table>")])
        self.assertLess(_t.time() - start, 5)
        self.assertEqual(saved, 1)                                   # just the gateway log
        self.assertIn("skipped the other 9", errors[-1])
        self.assertEqual([k for k, _ in Capturer(self.dir, {}).pages(name)], ["gateway_wan_log"])

    def test_quectel_without_login_says_so(self):
        _, _, errors = Capturer(self.dir, {"wan1": self.at_items()})("wan1")
        self.assertIn("QUECTEL_USER / QUECTEL_PASSWORD not set", errors[0])

    def cap(self, code):
        return Capturer(self.dir, {"wan2": [self.base + "public.ha", self.base + "syslog.ha"]}, {"wan2": code})

    def test_login_with_right_code_saves_both(self):
        name, saved, errors = self.cap(CODE)("wan2")
        self.assertEqual((saved, errors), (2, []))
        pages = dict(Capturer(self.dir, {}).files(name))
        self.assertIn("DSL line trained", pages["02-syslog.ha"])
        self.assertIn("Line State | Up", pages["01-public.ha"])

    def test_wrong_code_saves_public_page_only(self):
        _, saved, errors = self.cap("wrong")("wan2")
        self.assertEqual(saved, 1)
        self.assertIn("login failed", errors[0])
        self.assertEqual(STATE["logins"], 1)

    def test_no_code_never_tries_to_log_in(self):
        _, saved, errors = self.cap(None)("wan2")
        self.assertEqual(saved, 1)
        self.assertIn("access code (not set)", errors[0])
        self.assertEqual(STATE["logins"], 0)

    def test_bad_capture_names_rejected(self):
        self.assertIsNone(Capturer(self.dir, {}).files("../../etc"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
