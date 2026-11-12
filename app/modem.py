"""Modem health: signal per antenna, temperatures, supply voltage and PoE draw, kept in SQLite (/data/modem.db).

Where the numbers come from
- QManager (RM520N-GL): the sampler (main.py, every signal_sample_minutes) logs in, reads
  at_cmd/fetch_signal_history.sh (QManager's last ~30 min of per-antenna readings, one every 10-15 s, from RAM: no
  extra modem work) and at_cmd/fetch_data.sh (its status: temperature, carriers, antennas now), and logs out.
- The hourly saved modem pages: AT+QTEMP (CPU and ambient sensors), AT+CBC (supply voltage), fetch_data.sh, and
  the modem's own sensors read over SSH (case, amplifiers, supply rail), when that is set up.
- The UniFi gateway, every check: the modem port's PoE watts / volts / mA.

Checks (logged once when they start and when they clear)
- Hot: the modem's temperature (the higher of QManager's reading and the CPU sensor, last 90 min) at or above the
  warning / alert limits.
- Slowing down from heat: hot (>= modem_hot_c) and, on two readings in a row, fewer antennas reporting or fewer
  carriers than its usual count when cool (the most common count over the last 7 days below that temperature).
  The modem has no "throttling" flag, so this is the visible effect.
- PoE: the port's draw stepping up or down by poe_step_w for 5 minutes (a fan starting or stopping shows up as
  extra watts), or stopping (unplugged, on its 12 V supply, or PoE dropped).

Values meaning "no signal" (RSRP -140, SINR -20, -32768, null) are kept as NULL and shown as "-", never "Poor".
"""
import json
import os
import re
import sqlite3
import statistics
import subprocess
import threading
import time
from collections import deque

# ---------- signal quality (QManager types/modem-status.ts) ----------
SENTINELS = {"rsrp": {-140, -32768}, "rsrq": {-32768}, "sinr": {-20, -32768}}
QUALITY = {"rsrp": (-80, -100, -110, -120), "rsrq": (-5, -10, -15, -18), "sinr": (20, 13, 0, -10)}
BAR_RANGE = {"rsrp": (-140, -44), "rsrq": (-20, -3), "sinr": (-20, 30)}
UNITS = {"rsrp": "dBm", "rsrq": "dB", "sinr": "dB"}
PORTS = [("Main", "PRX"), ("Diversity", "DRX"), ("MIMO 3", "RX2"), ("MIMO 4", "RX3")]
RATS = (("lte", "LTE"), ("nr", "5G NR"))
METRICS = ("rsrp", "rsrq", "sinr")
SIG_COLS = [f"{r}_{m}{i}" for r, _ in RATS for m in METRICS for i in range(4)]
BEST_COLS = [f"{r}_{m}" for r, _ in RATS for m in METRICS]


def norm(v, metric):
    """A reading as a number, or None when the radio didn't measure anything."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f in SENTINELS[metric]:
        return None
    return int(f) if f == int(f) else f


def quality(v, metric):
    """excellent / good / fair / poor / bad, or none (no reading)."""
    if v is None:
        return "none"
    for name, floor in zip(("excellent", "good", "fair", "poor"), QUALITY[metric]):
        if v >= floor:
            return name
    return "bad"


def bar_pct(v, metric):
    if v is None:
        return 0
    lo, hi = BAR_RANGE[metric]
    return max(0, min(100, round(100 * (v - lo) / (hi - lo))))


def _arr(spa, key):
    v = (spa or {}).get(key)
    if isinstance(v, str):                              # "a, b, c, d" (flattened)
        v = [x.strip() for x in v.split(",")]
    v = list(v or [])[:4]
    return v + [None] * (4 - len(v))


def antennas(spa):
    """signal_per_antenna -> {"lte": {...}, "nr": {...}} for the Signal page: per port its readings, quality, bar
    width and verdict; per RAT the chains reporting and the best reading of each metric."""
    out = {}
    for rat, title in RATS:
        vals = {m: [norm(x, m) for x in _arr(spa, f"{rat}_{m}")] for m in METRICS}
        ports = []
        for i, (name, rx) in enumerate(PORTS):
            live = any(vals[m][i] is not None for m in METRICS)
            rows = [{"metric": m, "label": "SNR" if (m == "sinr" and rat == "nr") else m.upper(), "unit": UNITS[m],
                     "value": vals[m][i], "quality": quality(vals[m][i], m), "pct": bar_pct(vals[m][i], m)}
                    for m in METRICS]
            verdict = quality(vals["rsrp"][i], "rsrp") if vals["rsrp"][i] is not None else (
                "on" if live else "none")
            ports.append({"name": name, "rx": rx, "live": live, "rows": rows, "verdict": verdict})
        best = {m: max((x for x in vals[m] if x is not None), default=None) for m in METRICS}
        out[rat] = {"title": title, "ports": ports, "chains": sum(p["live"] for p in ports), "best": best,
                    "best_quality": {m: quality(best[m], m) for m in METRICS}}
    return out


def signal_row(entry):
    """A signal-history entry {"ts", "lte_rsrp": [4], ...} -> (ts, {column: value}) with best values and chains."""
    row = {}
    for rat, _ in RATS:
        chains = [False] * 4
        for m in METRICS:
            vals = [norm(x, m) for x in _arr(entry, f"{rat}_{m}")]
            for i, v in enumerate(vals):
                row[f"{rat}_{m}{i}"] = v
                chains[i] = chains[i] or v is not None
            row[f"{rat}_{m}"] = max((x for x in vals if x is not None), default=None)
        row[f"{rat}_chains"] = sum(chains)
    return int(float(entry.get("ts") or 0)), row


def carrier_count(details):
    """'B66: 20 MHz + B5: 10 MHz + N77: 60 MHz' -> 3."""
    return len([p for p in str(details or "").split("+") if p.strip()]) if details else 0


# ---------- storage ----------
class ModemStore:
    """signal: one row per antenna reading time (QManager's history), with each RAT's best value and chains.
    metrics: (wan, ts, name, value) for temperatures, voltage, PoE, carriers, chains."""

    def __init__(self, path, keep_days=45):
        self.path, self.keep_days = path, keep_days
        self.lock = threading.Lock()
        self.added = 0
        cols = ", ".join(f"{c} REAL" for c in SIG_COLS + BEST_COLS)
        with self._db() as db:
            db.execute(f"CREATE TABLE IF NOT EXISTS signal (wan TEXT, ts INTEGER, {cols}, lte_chains INTEGER, "
                       f"nr_chains INTEGER, PRIMARY KEY (wan, ts))")
            db.execute("CREATE TABLE IF NOT EXISTS metrics (wan TEXT, ts REAL, name TEXT, value REAL, "
                       "PRIMARY KEY (wan, name, ts))")
        self.prune()

    def _db(self):
        return sqlite3.connect(self.path, timeout=10)

    def prune(self):
        cutoff = time.time() - self.keep_days * 86400
        with self.lock, self._db() as db:
            db.execute("DELETE FROM signal WHERE ts < ?", (cutoff,))
            db.execute("DELETE FROM metrics WHERE ts < ?", (cutoff,))

    def _added(self, n):
        self.added += n
        if self.added >= 20000:
            self.added = 0
            self.prune()

    def add_signal(self, wan, entries):
        """QManager history entries; ones already stored (same ts) are skipped. Returns how many were new."""
        rows = [signal_row(e) for e in entries if isinstance(e, dict) and e.get("ts")]
        cols = SIG_COLS + BEST_COLS + ["lte_chains", "nr_chains"]
        sql = (f"INSERT OR IGNORE INTO signal (wan, ts, {', '.join(cols)}) "
               f"VALUES (?, ?, {', '.join('?' * len(cols))})")
        with self.lock, self._db() as db:
            before = db.total_changes
            db.executemany(sql, [[wan, ts] + [r.get(c) for c in cols] for ts, r in rows if ts])
            new = db.total_changes - before
        self._added(new)
        return new

    def add_metrics(self, wan, ts, values, replace=False):
        values = {k: v for k, v in values.items() if v is not None}
        if not values:
            return
        verb = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"
        with self.lock, self._db() as db:
            db.executemany(f"{verb} INTO metrics VALUES (?, ?, ?, ?)",
                           [(wan, ts, k, float(v)) for k, v in values.items()])
        self._added(len(values))

    def last_signal_ts(self, wan):
        with self.lock, self._db() as db:
            return db.execute("SELECT MAX(ts) FROM signal WHERE wan = ?", (wan,)).fetchone()[0]

    def latest(self, wan, name, since=0):
        """(ts, value) of the newest reading of one metric, or (None, None)."""
        with self.lock, self._db() as db:
            row = db.execute("SELECT ts, value FROM metrics WHERE wan = ? AND name = ? AND ts >= ? "
                             "ORDER BY ts DESC LIMIT 1", (wan, name, since)).fetchone()
        return row or (None, None)

    def values(self, wan, name, since):
        with self.lock, self._db() as db:
            return [r[0] for r in db.execute("SELECT value FROM metrics WHERE wan = ? AND name = ? AND ts >= ?",
                                             (wan, name, since))]

    def names(self, wan):
        with self.lock, self._db() as db:
            return [r[0] for r in db.execute("SELECT DISTINCT name FROM metrics WHERE wan = ?", (wan,))]

    @staticmethod
    def _step(since, until, points):
        return max(1, int((until - since) / max(1, points)))

    def series(self, wan, names, since, until=None, points=400):
        """{name: [[ts, avg, min, max], ...]} bucketed to about `points` points (ts = bucket start)."""
        until = until or time.time()
        step = self._step(since, until, points)
        out = {n: [] for n in names}
        if not names:
            return out
        q = ("SELECT name, CAST(ts / ? AS INTEGER) * ? AS b, AVG(value), MIN(value), MAX(value) FROM metrics "
             f"WHERE wan = ? AND ts >= ? AND ts <= ? AND name IN ({', '.join('?' * len(names))}) "
             "GROUP BY name, b ORDER BY b")
        with self.lock, self._db() as db:
            for name, b, avg, lo, hi in db.execute(q, [step, step, wan, since, until] + list(names)):
                out[name].append([b, round(avg, 2), round(lo, 2), round(hi, 2)])
        return out

    def signal_series(self, wan, since, until=None, points=400):
        """{column: [[ts, avg], ...]} of each RAT's best RSRP/RSRQ/SINR and chains, bucketed."""
        until = until or time.time()
        step = self._step(since, until, points)
        cols = BEST_COLS + ["lte_chains", "nr_chains"]
        q = (f"SELECT CAST(ts / ? AS INTEGER) * ? AS b, {', '.join(f'AVG({c})' for c in cols)} FROM signal "
             "WHERE wan = ? AND ts >= ? AND ts <= ? GROUP BY b ORDER BY b")
        out = {c: [] for c in cols}
        with self.lock, self._db() as db:
            for row in db.execute(q, (step, step, wan, since, until)):
                for c, v in zip(cols, row[1:]):
                    if v is not None:
                        out[c].append([row[0], round(v, 1)])
        return out

    def latest_signal(self, wan):
        """The newest stored antenna reading as signal_per_antenna-style lists, with its ts."""
        with self.lock, self._db() as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM signal WHERE wan = ? ORDER BY ts DESC LIMIT 1", (wan,)).fetchone()
        if not row:
            return None
        spa = {f"{r}_{m}": [row[f"{r}_{m}{i}"] for i in range(4)] for r, _ in RATS for m in METRICS}
        return dict(spa, ts=row["ts"])


# ---------- reading saved pages ----------
CELL_RE = re.compile(r"<tr[^>]*>\s*<td[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>\s*</tr>", re.S)


def table_values(page_html):
    """A saved Field | Value page -> {field: value} (first occurrence)."""
    import html as _html
    out = {}
    for k, v in CELL_RE.findall(page_html or ""):
        k, v = _html.unescape(re.sub("<[^>]+>", "", k)).strip(), _html.unescape(re.sub("<[^>]+>", "", v)).strip()
        out.setdefault(k, v)
    return out


def _num(v):
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def capture_metrics(pages):
    """{page key: html} of one save -> {metric: value} (QTEMP, CBC, QManager status, SSH sensors)."""
    m = {}
    for key, body in pages.items():
        vals = table_values(body)
        if "QTEMP" in key:
            m["cpu_temp"] = _num(vals.get("cpuss-0-usr"))
            m["ambient_temp"] = _num(vals.get("modem-ambient-usr"))
            pa = [_num(vals.get(k)) for k in ("modem-lte-sub6-pa1", "modem-lte-sub6-pa2")]
            pa = [x for x in pa if x is not None and -50 < x < 150]
            m["pa_temp"] = max(pa) if pa else None
        elif key.endswith("CBC"):
            m["supply_mv"] = _num(vals.get("supply voltage (mV)") or vals.get("CBC 3"))
        elif key.startswith("fetch_data"):
            m.update(status_metrics({k: v for k, v in vals.items()}))
        elif key == "modem_sensors":
            for k, v in vals.items():
                if k in SSH_SENSORS and _num(v) is not None:
                    m[SSH_SENSORS[k][0]] = _num(v)
    return {k: v for k, v in m.items() if v is not None}


def unflatten(flat):
    """{'a.b': v} (a saved status table) -> {'a': {'b': v}}; per-antenna lists stay "x, y" strings."""
    out = {}
    for k, v in flat.items():
        cur, parts = out, k.split(".")
        for p in parts[:-1]:
            nxt = cur.get(p)
            if not isinstance(nxt, dict):
                nxt = cur[p] = {}
            cur = nxt
        cur[parts[-1]] = v
    return out


def _dig(data, dotted):
    """data["a"]["b"] for 'a.b', or the flat key 'a.b' (saved pages are flattened)."""
    if dotted in data:
        return data[dotted]
    cur = data
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def status_metrics(data):
    """QManager fetch_data.sh (JSON, or its flattened saved table) -> {metric: value}."""
    spa = {f"{r}_{m}": _dig(data, f"signal_per_antenna.{r}_{m}") for r, _ in RATS for m in METRICS}
    ant = antennas(spa) if any(spa.values()) else None
    details = _dig(data, "network.bandwidth_details")
    return {"temp": _num(_dig(data, "device.temperature")), "cpu_usage": _num(_dig(data, "device.cpu_usage")),
            "carriers": carrier_count(details) if details else None,
            "bandwidth_mhz": _num(_dig(data, "network.total_bandwidth_mhz")),
            "lte_chains": ant["lte"]["chains"] if ant else None, "nr_chains": ant["nr"]["chains"] if ant else None,
            "lte_rsrp": _num(_dig(data, "lte.rsrp")), "nr_rsrp": _num(_dig(data, "nr.rsrp")),
            "nr_sinr": norm(_dig(data, "nr.sinr"), "sinr")}


# ---------- the modem's own sensors over SSH (read-only forced command, see modem/install-sensors-key.sh) ----------
# sysfs name -> (metric, label, scale): temps are in m°C, the supply rail in µV
SSH_SENSORS = {"pmx65_mdm_case_therm": ("case_temp", "Case", 1000),
               "pmx65_ambient_therm": ("board_temp", "Board", 1000),
               "pmk8350_xo_therm": ("xo_temp", "Crystal", 1000),
               "pmk8350_die_temp": ("pmic_temp", "Power chip", 1000),
               "pmx65_die_temp": ("pmx_temp", "Modem power chip", 1000),
               "pmx65_pa_therm1": ("pa1_temp", "Amplifier 1", 1000),
               "pmx65_pa_therm2": ("pa2_temp", "Amplifier 2", 1000),
               "vph_pwr": ("vph_mv", "Supply rail (mV)", 1000)}


def parse_sensors(text):
    """'in_temp_pmx65_mdm_case_therm_input:50600' lines -> {sysfs name: value in °C / mV}. Unused sensors
    (e.g. -40960 = no mmWave module) are dropped."""
    out = {}
    for ln in (text or "").splitlines():
        m = re.match(r"^(?:.*/)?in_(?:temp|voltage)_(.+?)_input:\s*(-?\d+)\s*$", ln.strip())
        if not m:
            continue
        name, raw = m.group(1), int(m.group(2))
        scale = SSH_SENSORS.get(name, (None, None, 1000))[2]
        v = round(raw / scale, 1)
        if "therm" in name or "temp" in name:
            if not -30 < v < 150:
                continue
        out[name] = v
    return out


class ModemSensors:
    """ssh root@<modem> with the watchdog's key; the modem's authorized_keys locks it to printing the sensors."""

    def __init__(self, host, key_file, known_hosts, timeout=20):
        self.host, self.key_file, self.known_hosts, self.timeout = host, key_file, known_hosts, timeout

    def configured(self):
        return bool(self.host) and os.path.exists(self.key_file)

    def read(self):
        """(sensors dict, error text or None)."""
        cmd = ["ssh", "-i", self.key_file, "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
               "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=accept-new",
               "-o", f"UserKnownHostsFile={self.known_hosts}", "-o", "LogLevel=ERROR", f"root@{self.host}", "sensors"]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout)
        except (OSError, subprocess.TimeoutExpired) as e:
            return {}, f"{e.__class__.__name__}: {e}"
        vals = parse_sensors(p.stdout)
        if p.returncode != 0 or not vals:
            err = " / ".join(x.strip() for x in (p.stderr or p.stdout).splitlines() if x.strip())[:200]
            return {}, err or f"no sensor values (exit {p.returncode})"
        return vals, None


def sensors_html(vals, error=None):
    from atparse import _grid_html
    if error:
        return _grid_html("Modem sensors (SSH)", ("Field", "Value"), [("error", error)], ("heading",))
    rows = [(k, v) for k, v in sorted(vals.items())]
    return _grid_html("Modem sensors (SSH): °C, supply rail in mV", ("Field", "Value"), rows, ("heading",))


# ---------- PoE draw ----------
POE_WINDOW = 35 * 60
POE_RECENT = 5 * 60


def poe_numbers(port):
    """UniFi port_table entry -> {"good", "power", "voltage", "current", "enabled", "mode"} (numbers as floats)."""
    if not port:
        return None
    return {"good": port.get("poe_good"), "power": _num(port.get("poe_power")),
            "voltage": _num(port.get("poe_voltage")), "current": _num(port.get("poe_current")),
            "enabled": port.get("poe_enable"), "mode": port.get("poe_mode"), "class": port.get("poe_class")}


class PoeWatch:
    """Per WAN: the last 35 min of PoE watts; reports a step (the last 5 min vs the 30 min before) once."""

    def __init__(self):
        self.samples = deque()

    def add(self, ts, watts, step_w):
        """Returns (old, new) watts when the draw stepped by at least step_w, else None."""
        if watts is None:
            return None
        self.samples.append((ts, watts))
        while self.samples and self.samples[0][0] < ts - POE_WINDOW:
            self.samples.popleft()
        if not step_w:
            return None
        recent = [w for t, w in self.samples if t >= ts - POE_RECENT]
        older = [w for t, w in self.samples if t < ts - POE_RECENT]
        if len(recent) < 5 or len(older) < 20:
            return None
        old, new = statistics.median(older), statistics.median(recent)
        if abs(new - old) < step_w:
            return None
        self.samples = deque((t, w) for t, w in self.samples if t >= ts - POE_RECENT)   # new baseline
        return round(old, 2), round(new, 2)


# ---------- the watcher ----------
class ModemWatch:
    """Ties it together for the monitor and the web pages. One instance; the QManager WAN is found from the
    capture settings (pages under /cgi-bin/quecmanager/)."""

    def __init__(self, store, events, settings, capture=None, sensors=None, clock=time.time):
        self.store, self.events, self.settings, self.capture, self.sensors = store, events, settings, capture, sensors
        self.clock = clock
        self.lock = threading.Lock()
        self.status = {}          # wan -> {"ts", "data": fetch_data JSON} (latest QManager status)
        self.heat = {}            # wan -> "ok" / "warn" / "alert"
        self.slow = {}            # wan -> {"active", "streak", "text"}
        self.poe = {}             # wan -> PoeWatch
        self.sample_error = {}    # wan -> last sampler error (logged once)
        self.last_sample = {}     # wan -> ts

    def _s(self, key, default):
        v = self.settings.get(key)
        return default if v is None else v

    # ----- which WAN has QManager -----
    def qm_wans(self):
        if not self.capture:
            return []
        return [k for k in sorted(self.capture.urls_by_wan) if self.capture.qm_base(k)]

    # ----- sampler (main.py thread) -----
    def sample(self, wan):
        """Read QManager's signal history and status once and store them. Returns (new rows, error)."""
        try:
            got = self.capture.qm_fetch(wan, ["at_cmd/fetch_signal_history.sh", "at_cmd/fetch_data.sh"])
        except Exception as e:                    # PermissionError / requests errors / RuntimeError
            return self._sample_failed(wan, str(e) or e.__class__.__name__)
        hist, status = got.get("at_cmd/fetch_signal_history.sh"), got.get("at_cmd/fetch_data.sh")
        new = 0
        try:
            entries = json.loads(hist) if hist else []
            if isinstance(entries, dict):
                entries = entries.get("data") or entries.get("history") or []
            new = self.store.add_signal(wan, entries if isinstance(entries, list) else [])
        except ValueError:
            pass
        now = self.clock()
        try:
            data = json.loads(status) if status else None
        except ValueError:
            data = None
        if isinstance(data, dict):
            self.ingest_status(wan, now, data)
        if self.sample_error.pop(wan, None):
            self.events.add("info", wan, "Modem signal sampler works again")
        self.last_sample[wan] = now
        return new, None

    def _sample_failed(self, wan, err):
        if self.sample_error.get(wan) != err:
            self.events.add("warn", wan, f"Modem signal sampler couldn't read QManager: {err}")
            self.sample_error[wan] = err
        return 0, err

    def ingest_status(self, wan, ts, data):
        with self.lock:
            self.status[wan] = {"ts": ts, "data": data}
        m = status_metrics(data)
        self.store.add_metrics(wan, ts, m)
        self.check(wan, ts, m)

    # ----- saved pages -----
    def sensor_page(self, wan):
        """[(page, html)] with the modem's SSH sensors for a save of this WAN, when that's turned on."""
        if wan not in self.qm_wans() or not self.settings.get("modem_ssh") or not self.sensors:
            return []
        if not self.sensors.configured():
            return [("modem_sensors", sensors_html({}, "no SSH key at /data/ssh/id_ed25519"))]
        vals, err = self.sensors.read()
        return [("modem_sensors", sensors_html(vals, err))]

    def ingest_capture(self, wan, name, ts=None, check=True):
        """Store the numbers from one saved set of pages. Never raises."""
        if wan not in self.qm_wans():
            return {}
        try:
            pages = {k: self.capture.raw(name, k) or "" for k, _ in (self.capture.pages(name) or [])}
            m = capture_metrics(pages)
            if not m:
                return {}
            ts = ts or self.capture.meta(name).get("time") or _name_time(name)
            if not ts:
                return {}
            self.store.add_metrics(wan, ts, m)
            status = next((unflatten(table_values(b)) for k, b in pages.items() if k.startswith("fetch_data")), None)
            if status:
                spa = status.get("signal_per_antenna")
                if isinstance(spa, dict) and spa:
                    self.store.add_signal(wan, [dict(spa, ts=int(ts))])     # one antenna reading per save
                with self.lock:
                    if ts > (self.status.get(wan) or {}).get("ts", 0):
                        self.status[wan] = {"ts": ts, "data": status}
            if check:
                self.check(wan, ts, m)
            return m
        except Exception:
            return {}

    def backfill(self):
        """At start: numbers from every save still on disk (the charts reach back 45 days at once)."""
        n = 0
        for wan in self.qm_wans():
            for name in self.capture.list(wan):
                if self.ingest_capture(wan, name, check=False):
                    n += 1
        return n

    # ----- checks -----
    def temperature(self, wan, now=None):
        """(°C, source) - the hottest of QManager's reading and the CPU sensor over the last 90 min."""
        now = now or self.clock()
        best = (None, None)
        for name, label in (("temp", "QManager"), ("cpu_temp", "CPU")):
            _ts, v = self.store.latest(wan, name, now - 5400)
            if v is not None and (best[0] is None or v > best[0]):
                best = (v, label)
        return best

    def check(self, wan, ts, m):
        self._check_heat(wan, ts)
        self._check_slow(wan, ts, m)

    def _check_heat(self, wan, ts):
        temp, src = self.temperature(wan, ts)
        if temp is None:
            return
        warn, alert = int(self._s("modem_temp_warn", 70)), int(self._s("modem_temp_alert", 75))
        old = self.heat.get(wan, "ok")
        if temp >= alert:
            new = "alert"
        elif temp >= warn:
            new = "warn"
        elif old != "ok" and temp > warn - 3:
            new = "warn" if old == "alert" else old        # small drop below the limit: not cleared yet
        else:
            new = "ok"
        if new == old:
            return
        self.heat[wan] = new
        if new == "alert":
            self.events.add("error", wan, f"Modem is VERY HOT: {temp:.0f} °C ({src}), at or above the {alert} °C "
                                          f"alert limit - Quectel's normal range ends near 75 °C")
        elif new == "warn" and old == "ok":
            self.events.add("warn", wan, f"Modem is hot: {temp:.0f} °C ({src}), at or above the {warn} °C warning")
        elif new == "warn":
            self.events.add("info", wan, f"Modem cooled to {temp:.0f} °C, below the {alert} °C alert limit")
        else:
            self.events.add("info", wan, f"Modem temperature back to normal: {temp:.0f} °C")

    def baseline(self, wan, now):
        """Usual (most common) chains and carriers over the last 7 days while the modem was below modem_hot_c."""
        hot = int(self._s("modem_hot_c", 60))
        with self.store.lock, self.store._db() as db:
            rows = db.execute(
                "SELECT m.name, m.value FROM metrics m JOIN metrics t ON t.wan = m.wan AND t.ts = m.ts "
                "AND t.name = 'temp' WHERE m.wan = ? AND m.ts >= ? AND t.value < ? "
                "AND m.name IN ('lte_chains', 'nr_chains', 'carriers')", (wan, now - 7 * 86400, hot)).fetchall()
        out = {}
        for name in ("lte_chains", "nr_chains", "carriers"):
            vals = [int(v) for n, v in rows if n == name]
            if len(vals) >= 6:
                out[name] = statistics.mode(vals)
        return out

    def _check_slow(self, wan, ts, m):
        if "temp" not in m:
            return
        st = self.slow.setdefault(wan, {"active": False, "streak": 0, "text": ""})
        hot = int(self._s("modem_hot_c", 60))
        lost = []
        if m["temp"] >= hot:
            base = self.baseline(wan, ts)
            for name, label in (("lte_chains", "LTE antennas"), ("nr_chains", "5G antennas"), ("carriers", "carriers")):
                if name in base and m.get(name) is not None and m[name] < base[name]:
                    lost.append(f"{label} {int(m[name])} (usually {base[name]})")
        st["streak"] = st["streak"] + 1 if lost else 0
        if lost and st["streak"] >= 2 and not st["active"]:
            st["active"] = True
            st["text"] = f"Modem may be slowing down from heat: {m['temp']:.0f} °C and " + ", ".join(lost)
            self.events.add("warn", wan, st["text"])
        elif not lost and st["active"]:
            st["active"], st["text"] = False, ""
            self.events.add("info", wan, f"Modem back to its usual antennas and carriers ({m['temp']:.0f} °C)")

    def poe_sample(self, wan, label, port_no, ts, poe):
        """Called by the monitor every check with poe_numbers(port)."""
        if not poe:
            return
        self.store.add_metrics(wan, ts, {"poe_w": poe["power"], "poe_v": poe["voltage"], "poe_ma": poe["current"]})
        pw = self.poe.setdefault(wan, PoeWatch())
        step = pw.add(ts, poe["power"], float(self._s("poe_step_w", 0.5) or 0))
        if not step:
            return
        old, new = step
        if new < 0.3 <= old:
            self.events.add("warn", wan, f"{label}'s modem stopped drawing PoE on port {port_no} ({old} W -> {new} W): "
                                         f"unplugged, running on its own 12 V supply, or PoE dropped")
        elif old < 0.3 <= new:
            self.events.add("info", wan, f"{label}'s modem draws PoE again on port {port_no}: {new} W")
        else:
            self.events.add("info", wan, f"{label}'s modem PoE draw changed {old} W -> {new} W "
                                         f"({new - old:+.2f} W for 5 min; a fan starting or stopping shows up here)")

    # ----- for the pages -----
    def snapshot(self, wan, now=None):
        now = now or self.clock()
        with self.lock:
            st = self.status.get(wan)
        data = (st or {}).get("data") or {}
        temp, src = self.temperature(wan, now)
        latest = {n: self.store.latest(wan, n, now - 7200)[1] for n in
                  ("cpu_temp", "ambient_temp", "supply_mv", "case_temp", "vph_mv", "poe_w", "poe_v", "poe_ma")}
        spa = data.get("signal_per_antenna") or self.store.latest_signal(wan)
        return {"wan": wan, "temp": temp, "temp_source": src, "heat": self.heat.get(wan, "ok"),
                "warn": int(self._s("modem_temp_warn", 70)), "alert": int(self._s("modem_temp_alert", 75)),
                "slow": (self.slow.get(wan) or {}).get("text") or "", "latest": latest,
                "status_age": int(now - st["ts"]) if st else None,
                "network": (data.get("network") or {}), "device": (data.get("device") or {}),
                "lte": data.get("lte") or {}, "nr": data.get("nr") or {},
                "antennas": antennas(spa) if spa else None,
                "sampler_error": self.sample_error.get(wan), "last_sample": self.last_sample.get(wan)}


def _name_time(name):
    try:
        return time.mktime(time.strptime(str(name)[:15], "%Y%m%d-%H%M%S"))
    except (ValueError, OverflowError):
        return None
