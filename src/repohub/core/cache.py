from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any, Callable


class Cache:
    def __init__(self, path: str = ":memory:", now: Callable[[], float] = time.time):
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self._lock = threading.Lock()
        self._now = now
        with self._lock:
            self._db.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT NOT NULL, expires REAL NOT NULL)")
            self._db.commit()

    def get(self, key: str, allow_stale: bool = False) -> Any | None:
        with self._lock:
            row = self._db.execute("SELECT value, expires FROM cache WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        if row[1] < self._now() and not allow_stale:
            return None
        return json.loads(row[0])

    def set(self, key: str, value: Any, ttl: float) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO cache (key, value, expires) VALUES (?, ?, ?)",
                (key, json.dumps(value), self._now() + ttl),
            )
            self._db.commit()
