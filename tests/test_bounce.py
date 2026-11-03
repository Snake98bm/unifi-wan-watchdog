"""Bouncer: a changed gateway host key gets a clear message and can be forgotten. Run: python tests/test_bounce.py"""
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from bounce import Bouncer  # noqa: E402


class BouncerTests(unittest.TestCase):
    def test_host_key_change_is_explained(self):
        b = Bouncer("gw", "/nonexistent", "/tmp/kh")
        fake = subprocess.CompletedProcess([], 255, "", "@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@\n"
                                                         "Host key verification failed.\n")
        with mock.patch("subprocess.run", return_value=fake):
            ok, detail = b.check()
        self.assertFalse(ok)
        self.assertIn("host key changed", detail)
        self.assertIn("Forget gateway host key", detail)

    def test_forget_host_key(self):
        kh = os.path.join(tempfile.mkdtemp(), "known_hosts")
        open(kh, "w").write("gw ssh-ed25519 AAAA...\n")
        b = Bouncer("gw", "/nonexistent", kh)
        b.forget_host_key()
        self.assertFalse(os.path.exists(kh))
        b.forget_host_key()                       # no file: still fine


if __name__ == "__main__":
    unittest.main(verbosity=2)
