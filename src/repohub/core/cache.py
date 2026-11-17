from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any, Callable


# Expired rows are kept this long because the hub serves them as a stale
# fallback (allow_stale=True) when a host is rate-limited or unreachable.
STALE_RETENTION = 7 * 24 * 3600
# Minimum time between opportunistic purges triggered by set().
PURGE_INTERVAL = 3600


class Cache:
    def __init__(self, path: str = ":memory:", now: Callable[[], float] = time.time,
                 retention: float = STALE_RETENTION):
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self._lock = threading.Lock()
        self._now = now
        self._retention = retention
        with self._lock:
            self._db.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT NOT NULL, expires REAL NOT NULL)")
            self._db.commit()
            self._purge_locked()

    def _purge_locked(self) -> int:
        """Delete rows expired for longer than the retention window. Caller holds the lock."""
        now = self._now()
        self._last_purge = now
        cur = self._db.execute("DELETE FROM cache WHERE expires < ?", (now - self._retention,))
        self._db.commit()
        return cur.rowcount

    def purge(self) -> int:
        """Remove rows past the stale-retention window; returns how many were deleted."""
        with self._lock:
            return self._purge_locked()

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
            if self._now() - self._last_purge >= PURGE_INTERVAL:
                self._purge_locked()

    def delete(self, key: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM cache WHERE key = ?", (key,))
            self._db.commit()
