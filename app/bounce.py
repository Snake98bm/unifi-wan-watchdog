"""Port bounce over SSH using the watchdog's restricted key.

On the gateway the key is locked to /data/wan-watchdog/wan-bounce.sh (see gateway/install-bounce.sh),
which only accepts: check | bounce <if> | bounce-long <if>.
"""
import os
import subprocess


class Bouncer:
    def __init__(self, host, key_file, known_hosts, timeout=75):
        self.host, self.key_file, self.known_hosts, self.timeout = host, key_file, known_hosts, timeout

    def configured(self):
        return os.path.exists(self.key_file)

    # exit codes of wan-bounce.sh; 255 = ssh itself failed (key refused, gateway unreachable)
    VERIFIED, REFUSED, NOT_BACK, NO_DROP = 0, 2, 3, 4

    def run(self, *args, raw=False):
        """Returns (exit code, output). raw=True keeps the output's lines as they are."""
        cmd = ["ssh", "-i", self.key_file, "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
               "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=accept-new",
               "-o", f"UserKnownHostsFile={self.known_hosts}", "-o", "LogLevel=ERROR",
               f"root@{self.host}", " ".join(args)]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout)
        except (OSError, subprocess.TimeoutExpired) as e:
            return 255, f"{e.__class__.__name__}: {e}"
        if raw:
            return p.returncode, p.stdout if p.returncode == 0 else (p.stdout + p.stderr).strip()
        out = " / ".join(line.strip() for line in (p.stdout + p.stderr).splitlines() if line.strip())
        return p.returncode, out or f"exit code {p.returncode}"

    def check(self):
        """(ok, firmware) - ok only if the restricted script answered."""
        code, out = self.run("check")
        if code == 0 and out.startswith("ok "):
            return True, out[3:].strip()
        return False, out

    def bounce(self, ifname):
        """(exit code, report). 0 = link dropped and came back up (verified on the gateway)."""
        return self.run("bounce", ifname)

    def wanlog(self, ifname):
        """(exit code, text): the gateway's recent syslog lines about this WAN port (read-only)."""
        return self.run("wanlog", ifname, raw=True)
