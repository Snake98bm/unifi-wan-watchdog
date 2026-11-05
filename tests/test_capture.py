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
STATE = {"nonce": None, "sessions": set(), "authed": set(), "logins": 0, "rebooted": False}
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

    def _json(self, data, status=200, cookie=None):
        import json
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def _qm_ok(self):
        return f"qm_session={STATE['qm_token']}" in self.headers.get("Cookie", "") and STATE["qm_token"]

    def qmanager(self, method):
        """Fake QManager: JSON login -> qm_session cookie; everything else needs the cookie."""
        import json
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)) if method == "POST" else b""
        path = self.path.split("/cgi-bin/quecmanager/", 1)[1]
        if path == "auth/login.sh":
            STATE["qm_logins"] += 1
            if json.loads(body).get("password") != "qm-pw":
                return self._json({"success": False, "error": "invalid_password", "attempts_remaining": 4})
            STATE["qm_token"] = secrets.token_hex(32)
            return self._json({"success": True}, cookie=f"qm_session={STATE['qm_token']}; HttpOnly; Path=/")
        if not self._qm_ok():
            return self._json({"success": False, "error": "unauthorized"}, 401)
        if path == "auth/logout.sh":
            STATE["qm_token"] = None
            return self._json({"success": True})
        if path == "at_cmd/send_command.sh":
            cmd = json.loads(body)["command"]
            STATE["at"].append(cmd)
            if cmd == "AT+BAD":
                return self._json({"success": False, "error": "command_failed", "command": cmd})
            return self._json({"success": True, "command": cmd, "response": f"{cmd}\r\r\n+CSQ: 22,99\r\n\r\nOK\r"})
        if path == "at_cmd/fetch_events.sh":
            return self._json([{"timestamp": 1793900000, "type": "band_change", "message": "NR band n77 -> n2 <x>",
                                "severity": "info"}])
        if path.startswith("at_cmd/fetch_data.sh"):
            return self._json({"device": {"uptime_seconds": 3600, "temperature": 41}, "lte": {"band": "B66"}})
        self.send_response(404)
        self.end_headers()

    def do_GET(self):
        sid = self._sid()
        if "/cgi-bin/quecmanager/" in self.path:
            return self.qmanager("GET")
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
        if self.path.endswith("restart.ha"):
            STATE["nonce"] = secrets.token_hex(8)
            if sid in STATE["authed"]:                  # authenticated: the real Restart page + button
                return self._send(f'<form method="post" action="/cgi-bin/restart.ha">'
                                  f'<input type="hidden" name="nonce" value="{STATE["nonce"]}" />'
                                  f'<input type="submit" name="Restart" value="Restart" /></form>')
            return self._send(f'<h1>Access Code Required</h1><form method="post" action="/cgi-bin/login.ha">'
                              f'<input type="hidden" name="nonce" value="{STATE["nonce"]}" /></form>')
        if sid in STATE["authed"]:
            return self._send("<table><tr><td>syslog</td><td>DSL line trained</td></tr></table>")
        STATE["nonce"] = secrets.token_hex(8)
        return self._send(f'<form method="post" action="/cgi-bin/login.ha">'
                          f'<input type="hidden" name="nonce" value="{STATE["nonce"]}" /></form>')

    def do_POST(self):
        if "/cgi-bin/quecmanager/" in self.path:
            return self.qmanager("POST")
        form = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
        if self.path.endswith("restart.ha"):           # the reboot trigger
            if self._sid() in STATE["authed"] and form.get("Restart"):
                STATE["rebooted"] = True
            return self._send("ok")
        STATE["logins"] += 1
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
        STATE.update(nonce=None, sessions=set(), authed=set(), logins=0, at=[], rebooted=False, qm_logins=0,
                     qm_token=None)
        self.dir = tempfile.mkdtemp()

    def host(self):
        return f"127.0.0.1:{self.server.server_address[1]}"

    def test_reboot_modem_authenticates_then_posts_restart(self):
        from capture import reboot_modem
        reboot_modem(self.host(), CODE)
        self.assertTrue(STATE["rebooted"])              # got through login + Restart POST
        self.assertEqual(STATE["logins"], 1)

    def test_reboot_wrong_code_raises_and_does_not_restart(self):
        from capture import reboot_modem
        with self.assertRaises(PermissionError):
            reboot_modem(self.host(), "wrong")
        self.assertFalse(STATE["rebooted"])

    def test_reboot_without_code_raises(self):
        from capture import reboot_modem
        with self.assertRaises(PermissionError):
            reboot_modem(self.host(), None)
        self.assertFalse(STATE["rebooted"])

    def test_capturer_reboot_uses_host_and_code(self):
        cap = Capturer(self.dir, {}, {"wan2": CODE}, reboot_hosts={"wan2": self.host()})
        self.assertTrue(cap.can_reboot("wan2"))
        self.assertFalse(cap.can_reboot("wan1"))        # no host/code for wan1
        cap.reboot("wan2")
        self.assertTrue(STATE["rebooted"])
        with self.assertRaises(RuntimeError):           # no host configured
            Capturer(self.dir, {}, {"wan2": CODE}).reboot("wan2")

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

    def qm_items(self, at="at:AT+CSQ\nat:AT+QENG=\"servingcell\""):
        import config
        qm = self.base + "quecmanager/"
        return config.capture_urls({"capture_wan1": f"{at}\n{qm}at_cmd/fetch_events.sh\n{qm}at_cmd/fetch_data.sh",
                                    "at_url_wan1": qm + "at_cmd/send_command.sh"})["wan1"]

    def test_qmanager_logs_in_runs_at_saves_pages_logs_out(self):
        cap = Capturer(self.dir, {"wan1": self.qm_items()}, auth_by_wan={"wan1": ("", "qm-pw")})
        name, saved, errors = cap("wan1")
        self.assertEqual((saved, errors), (4, []))
        self.assertEqual(STATE["qm_logins"], 1)                          # one login for the whole capture
        self.assertEqual(STATE["at"], ["AT+CSQ", 'AT+QENG="servingcell"'])   # sent as JSON, not URL-encoded
        self.assertIsNone(STATE["qm_token"])                             # logged out afterwards
        files = dict(Capturer(self.dir, {}).files(name))
        self.assertIn("RSSI (0-31) | 22", files["01-AT_CSQ"])
        self.assertIn("NR band n77 -> n2 <x>", files["03-fetch_events.sh"])    # JSON with '<' still a table
        self.assertIn("device.uptime_seconds | 3600", files["04-fetch_data.sh"])
        import pages
        header, rows = pages.merge_logs([(name, Capturer(self.dir, {}).raw(name, "fetch_events.sh"))])
        self.assertEqual(header[0], "Time")
        self.assertRegex(rows[0][0], r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")

    def test_qmanager_wrong_password_is_not_retried(self):
        cap = Capturer(self.dir, {"wan1": self.qm_items()}, auth_by_wan={"wan1": ("", "wrong")})
        _, saved, errors = cap("wan1")
        self.assertEqual((saved, STATE["qm_logins"]), (0, 1))
        self.assertIn("check QUECTEL_PASSWORD", errors[0])
        _, _, errors = cap("wan1")                                       # next hour: not sent again
        self.assertEqual(STATE["qm_logins"], 1)
        self.assertIn("not retried", errors[0])
        cap.auth_by_wan = {"wan1": ("", "qm-pw")}                        # fixed password: tried again
        self.assertEqual(cap("wan1")[1], 4)

    def test_qmanager_without_password_never_logs_in(self):
        _, saved, errors = Capturer(self.dir, {"wan1": self.qm_items()})("wan1")
        self.assertEqual((saved, STATE["qm_logins"]), (0, 0))
        self.assertIn("QUECTEL_PASSWORD not set", errors[0])

    def test_qmanager_refused_at_command_is_reported(self):
        cap = Capturer(self.dir, {"wan1": self.qm_items("at:AT+BAD\nat:AT+CSQ")}, auth_by_wan={"wan1": ("", "qm-pw")})
        _, saved, errors = cap("wan1")
        self.assertEqual(saved, 3)
        self.assertEqual(errors, ["AT_BAD: modem answered command_failed"])

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
