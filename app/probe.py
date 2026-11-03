"""Internet probe for ONE WAN.

Runs in its own small container on a VLAN that the gateway routes out of one WAN only (a UniFi policy-based
route with the kill switch on, so the VLAN has no internet at all when that WAN is dead). The container gets
its address by DHCP (probe-entry.sh) and listens on nothing.

Every PROBE_INTERVAL seconds it pings PROBE_TARGETS and asks Cloudflare (https://1.1.1.1/cdn-cgi/trace) which
public IP the request came from, then writes the result to PROBE_DIR/<PROBE_WAN>.json. The watchdog reads that
file; the exit IP lets it confirm the probe really went out through the WAN it is supposed to test.

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
from concurrent.futures import ThreadPoolExecutor

WAN = os.environ.get("PROBE_WAN", "wan1")
OUT_DIR = os.environ.get("PROBE_DIR", "/probes")
IFACE = os.environ.get("PROBE_IF", "eth0")
INTERVAL = max(10, int(os.environ.get("PROBE_INTERVAL", "30")))
TARGETS = [t for t in re.split(r"[\s,]+", os.environ.get("PROBE_TARGETS", "1.1.1.1 8.8.8.8")) if t][:4]
TRACE_HOST = "1.1.1.1"   # Cloudflare's trace page answers with the public IP the request came from
COUNT = 3                # pings per target and round


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
            if result["state"] != last:
                print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {WAN} {result['state']}: {result['detail']}"
                      f" (probe {result['ip'] or 'no IP'})", flush=True)
                last = result["state"]
        except Exception as e:   # keep probing no matter what
            print(f"probe round failed: {e}", flush=True)
        time.sleep(max(1.0, INTERVAL - (time.monotonic() - start)))


if __name__ == "__main__":
    sys.exit(health() if "--health" in sys.argv else main())
