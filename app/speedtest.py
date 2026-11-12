"""Scheduled speed tests per WAN, saved with what the line looked like at that moment.

The test itself runs in each WAN's probe container (probe.py), so it really goes out through that WAN: the
watchdog writes probes/<wan>.speedreq.json and reads the progress and result from probes/<wan>.speed.json. The
WANs are tested one after another, never at once (the VPN, when chosen, last: it rides on a WAN).

While the download runs, the watchdog notes the line: for the QManager (cellular) modem a fresh status read -
network type, carriers with their channel widths ("B66: 20 MHz + N77: 100 MHz"), total MHz, signal, temperature
(carriers are often only added under load, hence during the download); for the AT&T box the latest DSL sync
rates from the saved Broadband Status page. Results go to /data/speed.db (kept SPEED_KEEP_DAYS) so speeds can be
compared with channel width / sync rate over time.
"""
import json
import os
import re
import sqlite3
import threading
import time

from modem import status_metrics

SPEED_KEEP_DAYS = 365
TIMES_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
START_TIMEOUT = 90          # the probe looks for a request every 2 s, but may be mid-round
COLUMNS = ("ts", "wan", "label", "trigger", "state", "down_mbps", "up_mbps", "idle_ms", "down_ms", "up_ms",
           "data_mb", "colo", "exit_ip", "error", "link")


def parse_times(text):
    """'01:30, 4:30 19:00' -> [(1, 30), (4, 30), (19, 0)] (sorted, no repeats). Raises ValueError."""
    out = set()
    for part in re.split(r"[\s,;]+", str(text or "").strip()):
        if not part:
            continue
        m = TIMES_RE.match(part)
        if not m:
            raise ValueError(f"not a time (HH:MM): {part}")
        out.add((int(m.group(1)), int(m.group(2))))
    return sorted(out)


def fmt_times(times):
    return ", ".join(f"{h:02d}:{m:02d}" for h, m in times)


def cell_link(data):
    """The cellular line from QManager's fetch_data.sh JSON, for a speed-test row."""
    net = data.get("network") if isinstance(data.get("network"), dict) else {}
    m = status_metrics(data)
    return {"kind": "cell", "type": net.get("type"), "carriers": net.get("bandwidth_details"),
            "total_mhz": m.get("bandwidth_mhz"), "count": m.get("carriers"), "lte_rsrp": m.get("lte_rsrp"),
            "nr_rsrp": m.get("nr_rsrp"), "nr_sinr": m.get("nr_sinr"), "temp": m.get("temp")}


def dsl_link(dsl, now):
    """The bonded DSL line from the WAN's latest saved Broadband Status sample, for a speed-test row."""
    lines = [ln for ln in (dsl or {}).get("lines") or [] if ln]
    if not lines:
        return None
    up = [ln for ln in lines if (ln.get("state") or "").strip().lower() == "up"]
    return {"kind": "dsl", "sync_down": sum(ln.get("down") or 0 for ln in up),
            "sync_up": sum(ln.get("up") or 0 for ln in up), "pairs_up": len(up), "pairs": len(lines),
            "lines": [{k: ln.get(k) for k in ("line", "state", "down", "up", "ds_max", "us_max")} for ln in lines],
            "age_min": round((now - dsl["ts"]) / 60) if dsl.get("ts") else None}


def link_text(link):
    """One line for the page and the log."""
    if not link:
        return ""
    if link.get("kind") == "cell":
        parts = [p for p in (link.get("type"), link.get("carriers")) if p]
        if link.get("total_mhz"):
            parts.append(f"{link['total_mhz']:g} MHz total")
        return " · ".join(str(p) for p in parts)
    if link.get("kind") == "dsl":
        return (f"DSL sync {link['sync_down'] / 1000:.1f}/{link['sync_up'] / 1000:.1f} Mbps, "
                f"{link['pairs_up']} of {link['pairs']} pairs up")
    return ""


class SpeedStore:
    def __init__(self, path, keep_days=SPEED_KEEP_DAYS):
        self.path, self.keep_days = path, keep_days
        self.lock = threading.Lock()
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS speedtests (ts REAL, wan TEXT, label TEXT, trigger TEXT, "
                       "state TEXT, down_mbps REAL, up_mbps REAL, idle_ms REAL, down_ms REAL, up_ms REAL, "
                       "data_mb REAL, colo TEXT, exit_ip TEXT, error TEXT, link TEXT)")
            db.execute("CREATE INDEX IF NOT EXISTS speedtests_ts ON speedtests (ts)")
            db.execute("DELETE FROM speedtests WHERE ts < ?", (time.time() - keep_days * 86400,))

    def _db(self):
        return sqlite3.connect(self.path, timeout=10)

    def add(self, row):
        vals = [row.get(c) for c in COLUMNS]
        vals[-1] = json.dumps(row["link"]) if row.get("link") else None
        with self.lock, self._db() as db:
            db.execute(f"INSERT INTO speedtests VALUES ({', '.join('?' * len(COLUMNS))})", vals)

    def recent(self, since, wan=None, limit=1000):
        sql, args = "SELECT * FROM speedtests WHERE ts >= ?", [since]
        if wan:
            sql += " AND wan = ?"
            args.append(wan)
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        with self.lock, self._db() as db:
            rows = [dict(zip(COLUMNS, r)) for r in db.execute(sql, args)]
        for r in rows:
            try:
                r["link"] = json.loads(r["link"]) if r["link"] else None
            except ValueError:
                r["link"] = None
        return rows


def by_setup(rows):
    """Finished cellular tests grouped by carrier setup (bands + channel widths), most tests first:
    [{"wan", "label", "carriers", "total_mhz", "tests", "down", "up", "best_down", "per_mhz"}]."""
    groups = {}
    for r in rows:
        link = r.get("link") or {}
        if r["state"] != "done" or link.get("kind") != "cell" or not link.get("carriers"):
            continue
        g = groups.setdefault((r["wan"], link["carriers"]), {"wan": r["wan"], "label": r["label"],
                                                              "carriers": link["carriers"],
                                                              "total_mhz": link.get("total_mhz"), "d": [], "u": []})
        if r["down_mbps"] is not None:
            g["d"].append(r["down_mbps"])
        if r["up_mbps"] is not None:
            g["u"].append(r["up_mbps"])
    out = []
    for g in groups.values():
        d, u = g.pop("d"), g.pop("u")
        down = round(sum(d) / len(d), 1) if d else None
        out.append(dict(g, tests=max(len(d), len(u)), down=down, up=round(sum(u) / len(u), 1) if u else None,
                        best_down=max(d) if d else None,
                        per_mhz=round(down / g["total_mhz"], 2) if down and g["total_mhz"] else None))
    return sorted(out, key=lambda g: (g["wan"], -g["tests"]))


class SpeedTester:
    """Runs the tests (one WAN at a time, in a thread) and keeps the schedule."""

    def __init__(self, store, events, settings, monitor, modem=None, probe_dir=None, clock=time.time,
                 sleep=time.sleep):
        self.store, self.events, self.settings, self.monitor, self.modem = store, events, settings, monitor, modem
        self.probe_dir, self.clock, self.sleep = probe_dir, clock, sleep
        self.thread = None
        self.status = None        # {"wan", "label", "phase", "since", "trigger", "queue"} while running
        self.lock = threading.Lock()

    # ----- schedule -----
    def times(self):
        try:
            return parse_times(self.settings.get("speedtest_times"))
        except ValueError:
            return []

    def tick(self, now_dt):
        """Called about every 30 s: start the run for a scheduled time once. Returns True if one was started."""
        if not self.settings.get("speedtest_enabled") or not self.probe_dir:
            return False
        if (now_dt.hour, now_dt.minute) not in self.times():
            return False
        key = now_dt.strftime("%Y%m%d%H%M")
        if self.settings.get("speedtest_last") == key:
            return False
        self.settings.set(speedtest_last=key)          # mark first: a failed run isn't retried in a loop
        ok, _ = self.start(f"scheduled {now_dt:%H:%M}")
        return ok

    def next_run(self, now_dt):
        """'19:00' / 'tomorrow 01:30' for the page, or None."""
        times = self.times()
        if not times or not self.settings.get("speedtest_enabled"):
            return None
        now = (now_dt.hour, now_dt.minute)
        later = [t for t in times if t > now]
        return fmt_times(later[:1]) if later else "tomorrow " + fmt_times(times[:1])

    # ----- running -----
    def targets(self):
        """[(key, label)] to test: the enabled WANs (WAN 1 first), then the VPN when chosen and watched."""
        out = [(w.key, w.label) for w in sorted(self.monitor.wans, key=lambda w: w.key)
               if self.monitor._enabled(w.key)]
        if self.settings.get("speedtest_vpn") and self.settings.get("vpn_check"):
            out.append(("vpn", self.monitor.vpn_label))
        return out

    def running(self):
        return bool(self.thread and self.thread.is_alive())

    def start(self, trigger, only=None):
        """Start a run in the background. only = one key. Returns (ok, message)."""
        if not self.probe_dir:
            return False, "no probe folder - speed tests run in the probe containers"
        with self.lock:
            if self.running():
                return False, "a speed test is already running"
            targets = [t for t in self.targets() if only is None or t[0] == only]
            if not targets:
                return False, "nothing to test (WANs turned off?)"
            self.status = {"trigger": trigger, "since": self.clock(), "queue": [lbl for _, lbl in targets],
                           "wan": None, "label": None, "phase": "starting"}
            self.thread = threading.Thread(target=self._run_all, args=(targets, trigger), daemon=True,
                                           name="speedtest")
            self.thread.start()
        return True, f"Speed test started: {', '.join(lbl for _, lbl in targets)} (one after another)"

    def _run_all(self, targets, trigger):
        try:
            for key, label in targets:
                with self.lock:
                    self.status.update(wan=key, label=label, phase="waiting for the probe")
                try:
                    row = self.run_one(key, label, trigger)
                except Exception as e:             # one WAN's failure doesn't stop the others
                    row = self._row(key, label, trigger, "failed", error=f"{e.__class__.__name__}: {e}"[:200])
                self.store.add(row)
                self._log(row)
        finally:
            with self.lock:
                self.status = None

    def _row(self, key, label, trigger, state, **kw):
        return dict({c: None for c in COLUMNS}, ts=self.clock(), wan=key, label=label, trigger=trigger,
                    state=state, **kw)

    def _skip_reason(self, key):
        """Why this WAN can't be tested now, or None."""
        m = self.monitor
        with m.lock:
            if m.busy and m.busy.get("wan") == key:
                return f"a {m.busy.get('what') or 'reset'} is running on it"
            if key == "vpn":
                v = m.vpn
                return None if v.state in ("up", "banned") else f"VPN is {v.state}"
            w = next((w for w in m.wans if w.key == key), None)
            if w is None:
                return "unknown WAN"
            if not w.healthy:
                return f"WAN is {w.state}"
            if w.probe_verdict not in ("ok", "off"):
                return f"its internet probe isn't OK ({w.probe_verdict})"
        return None

    def _write_request(self, key, req):
        path = os.path.join(self.probe_dir, f"{key}.speedreq.json")
        with open(path + ".tmp", "w") as f:
            json.dump(req, f)
        os.replace(path + ".tmp", path)

    def _read_result(self, key):
        try:
            with open(os.path.join(self.probe_dir, f"{key}.speed.json")) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def _link(self, key):
        """The line right now: QManager status for the cellular modem, the latest DSL sample otherwise."""
        try:
            if self.modem and key in self.modem.qm_wans():
                self.modem.sample(key)
                st = (self.modem.status.get(key) or {})
                if st.get("data") and self.clock() - st.get("ts", 0) < 120:
                    return cell_link(st["data"])
                return None
            w = next((w for w in self.monitor.wans if w.key == key), None)
            if w is not None and w.dsl:
                return dsl_link(w.dsl, self.clock())
        except Exception:
            return None
        return None

    def run_one(self, key, label, trigger):
        why = self._skip_reason(key)
        if why:
            return self._row(key, label, trigger, "skipped", error=why, link=self._link(key))
        seconds = int(self.settings.get("speedtest_seconds") or 10)
        req = {"id": self.clock(), "seconds": seconds}
        self._write_request(key, req)
        give_up = req["id"] + START_TIMEOUT + 2 * (seconds + 2) + 60
        link, started = None, False
        while self.clock() < give_up:
            self.sleep(2)
            res = self._read_result(key)
            if not res or res.get("id") != req["id"]:
                if not started and self.clock() > req["id"] + START_TIMEOUT:
                    break
                continue
            started = True
            if res.get("state") == "running":
                with self.lock:
                    if self.status:
                        self.status["phase"] = {"ping": "idle ping", "down": "download",
                                                "up": "upload"}.get(res.get("phase"), res.get("phase"))
                if link is None and res.get("phase") == "down" \
                        and self.clock() - float(res.get("phase_started") or 0) >= min(4, seconds / 2):
                    link = self._link(key) or {}       # read once, while the download is running
                continue
            if not link:
                link = self._link(key)
            errors = "; ".join(res.get("errors") or [])
            return self._row(key, label, trigger, res.get("state") or "failed",
                             down_mbps=res.get("down_mbps"), up_mbps=res.get("up_mbps"), idle_ms=res.get("idle_ms"),
                             down_ms=res.get("down_ms"), up_ms=res.get("up_ms"),
                             data_mb=round((res.get("down_mb") or 0) + (res.get("up_mb") or 0), 1),
                             colo=res.get("colo"), exit_ip=res.get("exit_ip"),
                             error=res.get("error") or errors or None, link=link or None)
        what = "didn't finish in time" if started else "didn't start (probe container not running?)"
        return self._row(key, label, trigger, "failed", error=f"the probe's speed test {what}", link=link or None)

    def _log(self, row):
        name = f"Speed test {row['label']}"
        if row["state"] == "done":
            f = lambda v: f"{v:.0f}" if v is not None else "-"   # noqa: E731
            lat = f"idle {f(row['idle_ms'])} ms, loaded {f(row['down_ms'])}/{f(row['up_ms'])} ms"
            link = link_text(row.get("link"))
            self.events.add("info", row["wan"], f"{name}: {f(row['down_mbps'])} down / {f(row['up_mbps'])} up Mbps "
                                                f"({lat})" + (f" on {link}" if link else "")
                                                + (f" - {row['error']}" if row.get("error") else ""))
        elif row["state"] == "skipped":
            self.events.add("info", row["wan"], f"{name} skipped: {row['error']}")
        else:
            self.events.add("warn", row["wan"], f"{name} failed: {row['error']}")

    def snapshot(self):
        with self.lock:
            return dict(self.status) if self.status else None
