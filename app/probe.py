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

  python probe.py           run the probe loop
  python probe.py --health  container health check: exit 0 if the result file is fresh
"""
import http.client
import json
import os
import re
import ssl
import subprocess
import sys
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
    if result["state"] == "ok":          # sites can only be checked while the internet works
        result["ban"] = ban_check(read_conf(), time.time())
    return result


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
        time.sleep(max(1.0, INTERVAL - (time.monotonic() - start)))


if __name__ == "__main__":
    sys.exit(health() if "--health" in sys.argv else main())
