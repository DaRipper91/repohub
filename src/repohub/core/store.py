from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Callable

from repohub.core.models import Repo


class Favorites:
    def __init__(self, path: str = ":memory:", now: Callable[[], float] = time.time):
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self._lock = threading.Lock()
        self._now = now
        with self._lock:
            self._db.execute("CREATE TABLE IF NOT EXISTS favorites (key TEXT PRIMARY KEY, data TEXT NOT NULL, "
                             "added_at REAL NOT NULL, refreshed_at REAL NOT NULL)")
            self._db.commit()

    def add(self, repo: Repo) -> None:
        t = self._now()
        with self._lock:
            self._db.execute(
                "INSERT INTO favorites (key, data, added_at, refreshed_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET data = excluded.data, refreshed_at = excluded.refreshed_at",
                (repo.key, json.dumps(repo.to_dict()), t, t))
            self._db.commit()

    update = add

    def remove(self, key: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM favorites WHERE key = ?", (key,))
            self._db.commit()

    def is_favorite(self, key: str) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM favorites WHERE key = ?", (key,)).fetchone() is not None

    def list(self) -> list[Repo]:
        with self._lock:
            rows = self._db.execute("SELECT data FROM favorites ORDER BY added_at DESC, key").fetchall()
        return [Repo.from_dict(json.loads(r[0])) for r in rows]

    def stale(self, max_age: float) -> list[Repo]:
        cutoff = self._now() - max_age
        with self._lock:
            rows = self._db.execute("SELECT data FROM favorites WHERE refreshed_at < ?", (cutoff,)).fetchall()
        return [Repo.from_dict(json.loads(r[0])) for r in rows]
