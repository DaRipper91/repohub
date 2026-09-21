"""Small on/off switches kept in the local database (never anything secret)."""
from __future__ import annotations

import sqlite3
import threading

NAMES = ("guided_run",)


class Settings:
    def __init__(self, path: str = ":memory:"):
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
            self._db.commit()

    def get(self, name: str) -> bool:
        """Off unless explicitly turned on."""
        if name not in NAMES:
            raise ValueError("unknown setting")
        with self._lock:
            row = self._db.execute("SELECT value FROM settings WHERE name = ?", (name,)).fetchone()
        return row is not None and row[0] == "on"

    def set(self, name: str, on: bool) -> None:
        if name not in NAMES:
            raise ValueError("unknown setting")
        with self._lock:
            self._db.execute("INSERT INTO settings (name, value) VALUES (?, ?) "
                             "ON CONFLICT(name) DO UPDATE SET value = excluded.value", (name, "on" if on else "off"))
            self._db.commit()
