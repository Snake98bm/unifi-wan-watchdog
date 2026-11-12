"""Internet probe for ONE WAN (or for the VPN).

Runs in its own small container on a VLAN that the gateway routes out of one WAN only (a UniFi policy-based
route with the kill switch on, so the VLAN has no internet at all when that WAN is dead). The container gets
its address by DHCP (probe-entry.sh) and listens on nothing.

Every PROBE_INTERVAL seconds it pings PROBE_TARGETS and asks Cloudflare (https://1.1.1.1/cdn-cgi/trace) which
public IP the request came from, then writes the result to PROBE_DIR/<PROBE_WAN>.json. The watchdog reads that
file; the exit IP lets it confirm the probe really went out through the WAN it is supposed to test.

The VPN probe (PROBE_WAN=vpn, on a VLAN that the gateway routes through the VPN only) can also check that some
sites still let the VPN's exit IP in: the watchdog writes PROBE_DIR/<PROBE_WAN>.conf.json with
{"ban_urls": [...], "ban_every": seconds, "recheck": ts}, and every ban_every seconds (or as soon as "recheck" is
newer than the last check, e.g. right after the VPN reconnected) each URL is fetched and searched for
Cloudflare's "The owner of this website has banned your IP address" page (error 1006/1007/1008).

Speed test (on request): the watchdog writes PROBE_DIR/<PROBE_WAN>.speedreq.json with {"id": request time,
"seconds": N}. The probe notices within ~2 s and, in a thread (the normal probes keep running and are marked
"speedtest": true while it runs), measures against Cloudflare's speed test (speed.cloudflare.com, looked up over
DNS-over-HTTPS at 1.1.1.1 so the lookup leaves through this WAN too): idle ping, then N seconds of download and of
upload over SPEED_STREAMS connections each (the first SPEED_WARMUP seconds left out), with ping under load.
Progress and the result go to PROBE_DIR/<PROBE_WAN>.speed.json. A request older than SPEED_MAX_AGE is ignored.

  python probe.py           run the probe loop
  python probe.py --health  container health check: exit 0 if the result file is fresh
"""
import http.client
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

WAN = os.environ.get("PROBE_WAN", "wan1")
OUT_DIR = os.environ.get("PROBE_DIR", "/probes")
IFACE = os.environ.get("PROBE_IF", "eth0")
INTERVAL = max(10, int(os.environ.get("PROBE_INTERVAL", "30")))
TARGETS = [t for t in re.split(r"[\s,]+", os.environ.get("PROBE_TARGETS", "1.1.1.1 8.8.8.8")) if t][:4]
TRACE_HOST = "1.1.1.1"   # Cloudflare's trace page answers with the public IP the request came from
COUNT = 3                # pings per target and round
CONF = os.path.join(OUT_DIR, f"{WAN}.conf.json")
URL_RE = re.compile(r"^https?://[^\s/]+(/\S*)?$")
# Cloudflare's "IP banned by the site owner" page: the browser version says it in words, the plain one only
# gives the error code. A "Just a moment..." challenge is NOT a ban.
BAN_RE = re.compile(r"has banned your IP address|error code:?\s*100[678]\b|cf-error-code\">\s*100[678]\s*<", re.I)
BAN_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
_ban = {"ts": 0.0, "result": None}   # last ban check, kept between rounds
SPEED_REQ = os.path.join(OUT_DIR, f"{WAN}.speedreq.json")
SPEED_FILE = os.path.join(OUT_DIR, f"{WAN}.speed.json")
SPEED_HOST = "speed.cloudflare.com"
SPEED_DOWN_BYTES = 90_000_000     # per request; it refuses 100 MB or more
SPEED_UP_BYTES = 10_000_000       # per POST
SPEED_STREAMS = 6
SPEED_WARMUP = 2.0                # seconds of TCP ramp-up not counted
SPEED_MAX_AGE = 600               # an older request (e.g. found after a restart) is not run
SPEED_UA = "wan-watchdog-probe"
_speed = {"thread": None, "done_id": None}


def parse_ping(text):
    """(received, sent, average ms or None) from busybox or iputils ping output."""
    m = re.search(r"(\d+) packets transmitted, (\d+) (?:packets )?received", text)
    sent, recv = (int(m.group(1)), int(m.group(2))) if m else (COUNT, 0)
    avg = re.search(r"= [\d.]+/([\d.]+)/", text)
    return recv, sent, (float(avg.group(1)) if avg and recv else None)


def ping(target):
    try:
        p = subprocess.run(["ping", "-q", "-c", str(COUNT), "-W", "2", "-w", str(COUNT + 3), target],
                           capture_output=True, text=True, timeout=COUNT + 8)
        out = p.stdout + p.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        out = str(e)
    recv, sent, avg = parse_ping(out)
    return {"target": target, "sent": sent, "recv": recv, "avg_ms": avg}


def parse_trace(body):
    m = re.search(r"^ip=(\S+)$", body, re.M)
    return m.group(1) if m else None


def trace():
    start = time.monotonic()
    try:
        conn = http.client.HTTPSConnection(TRACE_HOST, timeout=6, context=ssl.create_default_context())
        conn.request("GET", "/cdn-cgi/trace", headers={"User-Agent": "wan-watchdog-probe"})
        body = conn.getresponse().read(4096).decode("ascii", "replace")
        conn.close()
    except (OSError, http.client.HTTPException) as e:
        return {"ok": False, "exit_ip": None, "ms": None, "error": f"{e.__class__.__name__}: {e}"[:200]}
    ip = parse_trace(body)
    return {"ok": bool(ip), "exit_ip": ip, "ms": round((time.monotonic() - start) * 1000, 1),
            "error": None if ip else "no ip= line in the answer"}


def read_conf():
    """What the watchdog asks for (ban-check URLs etc.); {} if there's no (valid) conf file."""
    try:
        with open(CONF) as f:
            c = json.load(f)
        urls = [u for u in c.get("ban_urls") or [] if isinstance(u, str) and URL_RE.match(u)][:4]
        return {"ban_urls": urls, "ban_every": max(60, int(c.get("ban_every") or 300)),
                "recheck": float(c.get("recheck") or 0)}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def parse_ban(status, text):
    """(banned, detail) for one fetched page."""
    if BAN_RE.search(text):
        m = re.search(r"banned your IP address \(([0-9a-fA-F.:]+)\)", text)
        return True, "Cloudflare: the site owner banned this IP" + (f" ({m.group(1)})" if m else "")
    challenge = "challenge-platform" in text or "Just a moment..." in text
    return False, f"HTTP {status}" + (" (Cloudflare challenge, not a ban)" if challenge else "")


def fetch_ban(url):
    start = time.monotonic()
    req = urllib.request.Request(url, headers={"User-Agent": BAN_UA, "Accept": "text/html,*/*;q=0.8"})
    try:
        try:
            with urllib.request.urlopen(req, timeout=10, context=ssl.create_default_context()) as r:
                status, body = r.status, r.read(65536)
        except urllib.error.HTTPError as e:          # Cloudflare answers a ban with 403: read its page
            status, body = e.code, e.read(65536)
    except (OSError, ValueError, http.client.HTTPException) as e:
        return {"url": url, "status": None, "banned": None, "ms": None,
                "detail": f"{e.__class__.__name__}: {e}"[:200]}
    banned, detail = parse_ban(status, body.decode("utf-8", "replace"))
    return {"url": url, "status": status, "banned": banned, "detail": detail,
            "ms": round((time.monotonic() - start) * 1000, 1)}


def ban_check(conf, now):
    """Latest ban check result (runs a new one when it's due), or None when no URLs are configured."""
    urls = conf.get("ban_urls") or []
    if not urls:
        _ban.update(ts=0.0, result=None)
        return None
    due = now - _ban["ts"] >= conf["ban_every"] or conf["recheck"] > _ban["ts"] or (
        _ban["result"] and [r["url"] for r in _ban["result"]["sites"]] != urls)
    if due:
        with ThreadPoolExecutor(len(urls)) as ex:
            sites = list(ex.map(fetch_ban, urls))
        _ban.update(ts=now, result={"ts": now, "banned": any(r["banned"] for r in sites), "sites": sites})
    return _ban["result"]


def own_ip():
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show", "dev", IFACE], capture_output=True, text=True,
                             timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)/", out)
    return m.group(1) if m else None


def summarize(pings, tr):
    """'ok' if anything answered, else 'fail', with a one-line detail."""
    sent = sum(p["sent"] for p in pings)
    recv = sum(p["recv"] for p in pings)
    rtts = [p["avg_ms"] for p in pings if p["avg_ms"] is not None]
    ok = recv > 0 or tr["ok"]
    parts = [f"{recv}/{sent} pings" + (f", {min(rtts):.0f} ms" if rtts else "")]
    parts.append(f"exit IP {tr['exit_ip']}" if tr["ok"] else "Cloudflare unreachable")
    return ("ok" if ok else "fail"), ", ".join(parts)


def probe_once():
    ip = own_ip()
    result = {"wan": WAN, "ts": time.time(), "ip": ip, "interval": INTERVAL, "targets": [], "trace": None}
    if not ip or ip.startswith("169.254."):
        result.update(state="no-lease", detail=f"no DHCP address on {IFACE} yet")
        return result
    with ThreadPoolExecutor(len(TARGETS) + 1) as ex:
        tr = ex.submit(trace)
        result["targets"] = list(ex.map(ping, TARGETS))
        result["trace"] = tr.result()
    result["state"], result["detail"] = summarize(result["targets"], result["trace"])
    if speedtest_running():              # the link is saturated on purpose: the watchdog leaves these out
        result["speedtest"] = True
    if result["state"] == "ok":          # sites can only be checked while the internet works
        result["ban"] = ban_check(read_conf(), time.time())
    return result


# ---------- speed test ----------
class _Deadline(Exception):
    pass


class PinnedHTTPS(http.client.HTTPSConnection):
    """HTTPS to `host` (certificate and SNI checked for it) at an address looked up beforehand."""

    def __init__(self, host, ip, timeout=10):
        super().__init__(host, timeout=timeout, context=ssl.create_default_context())
        self._ip = ip

    def connect(self):
        sock = socket.create_connection((self._ip, 443), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def parse_doh(body):
    """IPv4 addresses from a DNS-over-HTTPS JSON answer."""
    try:
        data = json.loads(body)
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []
    return [a["data"] for a in data.get("Answer") or []
            if isinstance(a, dict) and a.get("type") == 1 and re.fullmatch(r"\d+\.\d+\.\d+\.\d+", str(a.get("data")))]


def resolve(host):
    """Look `host` up at Cloudflare's DNS-over-HTTPS (https://1.1.1.1), so the lookup uses this WAN as well."""
    conn = http.client.HTTPSConnection(TRACE_HOST, timeout=6, context=ssl.create_default_context())
    try:
        conn.request("GET", f"/dns-query?name={host}&type=A",
                     headers={"Accept": "application/dns-json", "User-Agent": SPEED_UA})
        return parse_doh(conn.getresponse().read(8192).decode("utf-8", "replace"))
    finally:
        conn.close()


def ping_for(seconds, target="1.1.1.1"):
    """Average ping over `seconds` (one a second), or None."""
    n = max(2, int(seconds))
    try:
        p = subprocess.run(["ping", "-q", "-c", str(n), "-W", "2", "-w", str(n + 2), target],
                           capture_output=True, text=True, timeout=n + 6)
        return parse_ping(p.stdout + p.stderr)[2]
    except (OSError, subprocess.TimeoutExpired):
        return None


def mbps(nbytes, seconds):
    return round(nbytes * 8 / seconds / 1e6, 1) if seconds > 0 else None


def _upload_body(deadline, add):
    block = b"\0" * 65536
    left = SPEED_UP_BYTES
    while left > 0:
        if time.monotonic() >= deadline:
            raise _Deadline
        n = min(len(block), left)
        yield block[:n]
        add(n)
        left -= n


def transfer(kind, ip, seconds, streams=SPEED_STREAMS, warmup=SPEED_WARMUP, conn_cls=None):
    """One direction ("down" / "up") for warmup + seconds over `streams` connections.
    Returns {"mbps", "mb", "errors", "colo"}: mbps counts only the bytes after the warm-up."""
    conn_cls = conn_cls or PinnedHTTPS
    lock = threading.Lock()
    total = [0]
    errors, colos = [], []

    def add(n):
        with lock:
            total[0] += n

    start = time.monotonic()
    deadline = start + warmup + seconds

    def worker():
        conn = None
        while time.monotonic() < deadline:
            try:
                if conn is None:
                    conn = conn_cls(SPEED_HOST, ip)
                if kind == "down":
                    conn.request("GET", f"/__down?bytes={SPEED_DOWN_BYTES}", headers={"User-Agent": SPEED_UA})
                    r = conn.getresponse()
                    if r.getheader("colo"):
                        colos.append(r.getheader("colo"))
                    if r.status != 200:
                        errors.append(f"HTTP {r.status}")
                        r.read(4096)
                        break
                    while True:
                        if time.monotonic() >= deadline:
                            raise _Deadline
                        b = r.read(65536)
                        if not b:
                            break
                        add(len(b))
                else:
                    conn.request("POST", "/__up", body=_upload_body(deadline, add),
                                 headers={"User-Agent": SPEED_UA, "Content-Type": "application/octet-stream",
                                          "Content-Length": str(SPEED_UP_BYTES)})
                    r = conn.getresponse()
                    r.read(4096)
                    if r.status != 200:
                        errors.append(f"HTTP {r.status}")
                        break
            except _Deadline:
                break
            except (OSError, http.client.HTTPException) as e:
                errors.append(f"{e.__class__.__name__}: {e}"[:120])
                if conn:
                    conn.close()
                conn = None
                time.sleep(0.5)
        if conn:
            conn.close()

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(streams)]
    for t in threads:
        t.start()
    time.sleep(warmup)
    with lock:
        b0, t0 = total[0], time.monotonic()
    time.sleep(max(0.0, deadline - time.monotonic()))
    with lock:
        b1, t1 = total[0], time.monotonic()
    for t in threads:
        t.join(15)
    return {"mbps": mbps(b1 - b0, t1 - t0) if b1 > b0 else None, "mb": round(total[0] / 1e6, 1),
            "errors": sorted(set(errors))[:5], "colo": colos[0] if colos else None}


def write_speed(result):
    tmp = SPEED_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(result, f)
    os.replace(tmp, SPEED_FILE)


def read_speed_request():
    """{"id", "seconds"} the watchdog asked for, or None."""
    try:
        with open(SPEED_REQ) as f:
            c = json.load(f)
        return {"id": float(c["id"]), "seconds": min(30, max(5, int(c.get("seconds") or 10)))}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


def run_speedtest(req):
    out = {"id": req["id"], "wan": WAN, "started": time.time(), "state": "running", "phase": "ping",
           "seconds": req["seconds"]}
    write_speed(out)
    try:
        ips = resolve(SPEED_HOST)
        if not ips:
            raise RuntimeError(f"couldn't look up {SPEED_HOST}")
        out["server_ip"] = ips[0]
        out["idle_ms"] = ping_for(5)
        for kind in ("down", "up"):
            out["phase"] = kind
            out["phase_started"] = time.time()
            write_speed(out)
            with ThreadPoolExecutor(1) as ex:
                loaded = ex.submit(ping_for, SPEED_WARMUP + req["seconds"])
                r = transfer(kind, ips[0], req["seconds"])
                out[f"{kind}_ms"] = loaded.result()
            out[f"{kind}_mbps"], out[f"{kind}_mb"] = r["mbps"], r["mb"]
            out["colo"] = out.get("colo") or r["colo"]
            if r["errors"]:
                out.setdefault("errors", []).extend(f"{kind}: {e}" for e in r["errors"])
        out["exit_ip"] = trace()["exit_ip"]
        if out.get("down_mbps") is None and out.get("up_mbps") is None:
            out.update(state="failed", error="; ".join(out.get("errors") or []) or "no data moved")
        else:
            out["state"] = "done"
    except Exception as e:                       # report anything, never kill the probe
        out.update(state="failed", error=f"{e.__class__.__name__}: {e}"[:200])
    out.pop("phase", None)
    out.pop("phase_started", None)
    out["finished"] = time.time()
    write_speed(out)
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {WAN} speed test {out['state']}: "
          f"down {out.get('down_mbps')} / up {out.get('up_mbps')} Mbps"
          + (f" ({out['error']})" if out.get("error") else ""), flush=True)
    return out


def speedtest_running():
    t = _speed["thread"]
    return bool(t and t.is_alive())


def maybe_speedtest(now=None):
    """Start a requested speed test in the background. Returns True if one was started."""
    if _speed["done_id"] is None:              # first look: what was already run before a restart
        try:
            with open(SPEED_FILE) as f:
                _speed["done_id"] = float(json.load(f).get("id") or 0)
        except (OSError, ValueError, TypeError, AttributeError):
            _speed["done_id"] = 0.0
    req = read_speed_request()
    if not req or req["id"] <= _speed["done_id"] or speedtest_running():
        return False
    _speed["done_id"] = req["id"]
    if (now or time.time()) - req["id"] > SPEED_MAX_AGE:
        return False
    _speed["thread"] = threading.Thread(target=run_speedtest, args=(req,), daemon=True, name="speedtest")
    _speed["thread"].start()
    return True


def write(result):
    path = os.path.join(OUT_DIR, f"{WAN}.json")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(result, f)
    os.replace(tmp, path)


def health():
    try:
        age = time.time() - os.path.getmtime(os.path.join(OUT_DIR, f"{WAN}.json"))
    except OSError:
        return 1
    return 0 if age < 3 * INTERVAL + 30 else 1


def main():
    print(f"wan-watchdog probe for {WAN}: {', '.join(TARGETS)} + Cloudflare trace every {INTERVAL} s", flush=True)
    last = None
    while True:
        start = time.monotonic()
        try:
            result = probe_once()
            write(result)
            state = result["state"] + (" BANNED" if (result.get("ban") or {}).get("banned") else "")
            if state != last:
                print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {WAN} {state}: {result['detail']}"
                      f" (probe {result['ip'] or 'no IP'})", flush=True)
                last = state
        except Exception as e:   # keep probing no matter what
            print(f"probe round failed: {e}", flush=True)
        end = start + max(1.0, INTERVAL - (time.monotonic() - start))
        while True:                      # look for a speed-test request every 2 s while waiting
            try:
                maybe_speedtest()
            except Exception as e:
                print(f"speed test start failed: {e}", flush=True)
            left = end - time.monotonic()
            if left <= 0:
                break
            time.sleep(min(2.0, left))


if __name__ == "__main__":
    sys.exit(health() if "--health" in sys.argv else main())
