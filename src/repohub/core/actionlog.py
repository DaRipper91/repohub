"""A small local log of the write actions RepoHub performed (star, unstar, fork).

It holds the host, repository, action, time and a short result only: never a token or a response body.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Callable

from repohub.core.textsafe import clean_text

MAX_ROWS = 200
MAX_RESULT = 200
ACTIONS = ("star", "unstar", "fork")


@dataclass(frozen=True)
class ActionEntry:
    host: str
    slug: str
    action: str
    at: float
    ok: bool
    result: str


class ActionLog:
    def __init__(self, path: str = ":memory:", now: Callable[[], float] = time.time):
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self._lock = threading.Lock()
        self._now = now
        with self._lock:
            self._db.execute("CREATE TABLE IF NOT EXISTS actions (id INTEGER PRIMARY KEY AUTOINCREMENT, host TEXT NOT NULL, "
                             "slug TEXT NOT NULL, action TEXT NOT NULL, at REAL NOT NULL, ok INTEGER NOT NULL, result TEXT NOT NULL)")
            self._db.commit()

    def add(self, host: str, slug: str, action: str, ok: bool, result: str) -> None:
        if action not in ACTIONS:
            raise ValueError("unknown action")
        with self._lock:
            self._db.execute("INSERT INTO actions (host, slug, action, at, ok, result) VALUES (?, ?, ?, ?, ?, ?)",
                             (clean_text(host)[:40], clean_text(slug)[:200], action, self._now(), int(ok),
                              clean_text(result)[:MAX_RESULT]))
            self._db.execute("DELETE FROM actions WHERE id NOT IN (SELECT id FROM actions ORDER BY id DESC LIMIT ?)",
                             (MAX_ROWS,))
            self._db.commit()

    def recent(self, limit: int = 20) -> list[ActionEntry]:
        with self._lock:
            rows = self._db.execute("SELECT host, slug, action, at, ok, result FROM actions ORDER BY id DESC LIMIT ?",
                                    (max(1, min(limit, MAX_ROWS)),)).fetchall()
        return [ActionEntry(r[0], r[1], r[2], r[3], bool(r[4]), r[5]) for r in rows]
