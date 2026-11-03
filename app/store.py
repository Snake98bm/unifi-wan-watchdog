"""Event log (SQLite) and persisted settings (JSON) under /data."""
import json
import os
import sqlite3
import threading
import time


class Events:
    def __init__(self, path, keep_days=180):
        self.path, self.keep_days = path, keep_days
        self.lock = threading.Lock()
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS events (ts REAL, level TEXT, wan TEXT, message TEXT)")
            db.execute("CREATE INDEX IF NOT EXISTS events_ts ON events (ts)")
            db.execute("DELETE FROM events WHERE ts < ?", (time.time() - keep_days * 86400,))

    def _db(self):
        return sqlite3.connect(self.path, timeout=10)

    def add(self, level, wan, message):
        ts = time.time()
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts))} [{level}] {message}", flush=True)
        with self.lock, self._db() as db:
            db.execute("INSERT INTO events VALUES (?, ?, ?, ?)", (ts, level, wan, message))

    def recent(self, limit=500, wan=None, level=None):
        sql, args = "SELECT ts, level, wan, message FROM events WHERE 1=1", []
        if wan:
            sql += " AND wan = ?"
            args.append(wan)
        if level:
            sql += " AND level = ?"
            args.append(level)
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        with self.lock, self._db() as db:
            return [dict(zip(("ts", "level", "wan", "message"), row)) for row in db.execute(sql, args)]


class Probes:
    """History of the internet probes (one row per probe result per WAN), for the Internet page."""

    def __init__(self, path, keep_days=30):
        self.path, self.keep_days = path, keep_days
        self.lock = threading.Lock()
        self.added = 0
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS probes (ts REAL, wan TEXT, verdict TEXT, sent INTEGER, "
                       "recv INTEGER, avg_ms REAL, exit_ip TEXT)")
            db.execute("CREATE INDEX IF NOT EXISTS probes_ts ON probes (ts)")
        self.prune()

    def _db(self):
        return sqlite3.connect(self.path, timeout=10)

    def prune(self):
        with self.lock, self._db() as db:
            db.execute("DELETE FROM probes WHERE ts < ?", (time.time() - self.keep_days * 86400,))

    def add(self, ts, wan, verdict, sent, recv, avg_ms, exit_ip):
        with self.lock, self._db() as db:
            db.execute("INSERT INTO probes VALUES (?, ?, ?, ?, ?, ?, ?)", (ts, wan, verdict, sent, recv, avg_ms, exit_ip))
        self.added += 1
        if self.added % 2000 == 0:          # about once a day per WAN at one result every 30 s
            self.prune()

    def hourly(self, wan, since):
        """[{hour, checks, sent, recv, loss, avg_ms, worst_ms, failed}] newest first (hour = epoch of its start)."""
        sql = ("SELECT CAST(ts / 3600 AS INTEGER) * 3600 AS hour, COUNT(*), SUM(sent), SUM(recv), AVG(avg_ms), "
               "MAX(avg_ms), SUM(verdict = 'fail') FROM probes WHERE wan = ? AND ts >= ? "
               "GROUP BY hour ORDER BY hour DESC")
        with self.lock, self._db() as db:
            rows = db.execute(sql, (wan, since)).fetchall()
        out = []
        for hour, checks, sent, recv, avg, worst, failed in rows:
            out.append({"hour": hour, "checks": checks, "sent": sent or 0, "recv": recv or 0,
                        "loss": round(100 * (1 - (recv or 0) / sent), 1) if sent else None,
                        "avg_ms": round(avg, 1) if avg is not None else None,
                        "worst_ms": round(worst, 1) if worst is not None else None, "failed": failed or 0})
        return out


class Settings:
    def __init__(self, path, defaults):
        self.path = path
        self.lock = threading.Lock()
        self.data = dict(defaults)
        if os.path.exists(path):
            with open(path) as f:
                self.data.update(json.load(f))

    def get(self, key):
        return self.data.get(key)

    def set(self, **values):
        with self.lock:
            self.data.update(values)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.data, f)
            os.replace(tmp, self.path)
