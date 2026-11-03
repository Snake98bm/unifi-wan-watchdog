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
