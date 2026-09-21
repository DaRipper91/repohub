"""Opt-in local history of repositories opened. Off by default; capped by count and age; clearable."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Callable

from repohub.core.models import Repo

MAX_ENTRIES = 500
MAX_AGE = 90 * 86400


class History:
    def __init__(self, path: str = ":memory:", now: Callable[[], float] = time.time):
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self._lock = threading.Lock()
        self._now = now
        with self._lock:
            self._db.execute("CREATE TABLE IF NOT EXISTS history (key TEXT PRIMARY KEY, data TEXT NOT NULL, at REAL NOT NULL)")
            self._db.execute("CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
            self._db.commit()

    @property
    def enabled(self) -> bool:
        with self._lock:
            row = self._db.execute("SELECT value FROM settings WHERE name = 'history'").fetchone()
        return row is not None and row[0] == "on"

    def set_enabled(self, on: bool) -> None:
        with self._lock:
            self._db.execute("INSERT INTO settings (name, value) VALUES ('history', ?) "
                             "ON CONFLICT(name) DO UPDATE SET value = excluded.value", ("on" if on else "off",))
            self._db.commit()

    def record(self, repo: Repo) -> None:
        """Remember a repository that was opened. Does nothing while history is off."""
        if not self.enabled:
            return
        t = self._now()
        with self._lock:
            self._db.execute("INSERT INTO history (key, data, at) VALUES (?, ?, ?) "
                             "ON CONFLICT(key) DO UPDATE SET data = excluded.data, at = excluded.at",
                             (repo.key, json.dumps(repo.to_dict()), t))
            self._db.execute("DELETE FROM history WHERE at < ?", (t - MAX_AGE,))
            self._db.execute("DELETE FROM history WHERE key NOT IN (SELECT key FROM history ORDER BY at DESC LIMIT ?)",
                             (MAX_ENTRIES,))
            self._db.commit()

    def list(self) -> list[Repo]:
        """Newest first. Nothing is returned while history is off, whatever the table holds."""
        if not self.enabled:
            return []
        with self._lock:
            rows = self._db.execute("SELECT data FROM history WHERE at >= ? ORDER BY at DESC",
                                    (self._now() - MAX_AGE,)).fetchall()
        out = []
        for row in rows:
            try:
                out.append(Repo.from_dict(json.loads(row[0])))
            except Exception:  # a damaged row is skipped, never fatal
                continue
        return out

    def clear(self) -> None:
        with self._lock:
            self._db.execute("DELETE FROM history")
            self._db.commit()
