from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from typing import Callable

from repohub.core.models import Repo
from repohub.core.textsafe import clean_text

TAG_RE = re.compile(r"^[a-z0-9][a-z0-9 _-]{0,29}$")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,39}$")
MAX_TAGS = 10
MAX_NOTE = 500
MAX_COLLECTIONS = 50
MAX_PER_COLLECTION = 500


class Favorites:
    def __init__(self, path: str = ":memory:", now: Callable[[], float] = time.time):
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self._lock = threading.Lock()
        self._now = now
        with self._lock:
            self._db.execute("CREATE TABLE IF NOT EXISTS favorites (key TEXT PRIMARY KEY, data TEXT NOT NULL, "
                             "added_at REAL NOT NULL, refreshed_at REAL NOT NULL)")
            for ddl in (
                "CREATE TABLE IF NOT EXISTS fav_note (key TEXT PRIMARY KEY, note TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS fav_tag (key TEXT NOT NULL, tag TEXT NOT NULL, PRIMARY KEY (key, tag))",
                "CREATE TABLE IF NOT EXISTS collection (name TEXT PRIMARY KEY, created REAL NOT NULL)",
                "CREATE TABLE IF NOT EXISTS collection_item (name TEXT NOT NULL, key TEXT NOT NULL, PRIMARY KEY (name, key))",
                "CREATE TABLE IF NOT EXISTS fav_release (key TEXT PRIMARY KEY, latest_tag TEXT NOT NULL, "
                "published_at TEXT, checked_at REAL NOT NULL, seen_tag TEXT)"):
                self._db.execute(ddl)
            self._db.commit()

    def add(self, repo: Repo) -> None:
        t = self._now()
        with self._lock:
            self._db.execute(
                "INSERT INTO favorites (key, data, added_at, refreshed_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET data = excluded.data, refreshed_at = excluded.refreshed_at",
                (repo.key, json.dumps(repo.to_dict()), t, t))
            self._db.commit()

    def update(self, repo: Repo) -> None:
        with self._lock:
            self._db.execute("UPDATE favorites SET data = ?, refreshed_at = ? WHERE key = ?",
                             (json.dumps(repo.to_dict()), self._now(), repo.key))
            self._db.commit()

    def mark_checked(self, key: str) -> None:
        """Record a refresh attempt for an existing row without changing its data."""
        with self._lock:
            self._db.execute("UPDATE favorites SET refreshed_at = ? WHERE key = ?", (self._now(), key))
            self._db.commit()

    def remove(self, key: str) -> None:
        with self._lock:
            for table, col in (("favorites", "key"), ("fav_note", "key"), ("fav_tag", "key"),
                               ("collection_item", "key"), ("fav_release", "key")):
                self._db.execute(f"DELETE FROM {table} WHERE {col} = ?", (key,))
            self._db.commit()

    def is_favorite(self, key: str) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM favorites WHERE key = ?", (key,)).fetchone() is not None

    def list(self, tag: str | None = None, collection: str | None = None, q: str | None = None) -> list[Repo]:
        """Favorites, newest first; optionally only those with a tag, in a collection, or matching text."""
        sql = "SELECT f.data FROM favorites f"
        where, args = [], []
        if tag:
            where.append("EXISTS (SELECT 1 FROM fav_tag t WHERE t.key = f.key AND t.tag = ?)")
            args.append(tag)
        if collection:
            where.append("EXISTS (SELECT 1 FROM collection_item c WHERE c.key = f.key AND c.name = ?)")
            args.append(collection)
        if where:
            sql += " WHERE " + " AND ".join(where)
        with self._lock:
            rows = self._db.execute(sql + " ORDER BY f.added_at DESC, f.key", args).fetchall()
        repos = [Repo.from_dict(json.loads(r[0])) for r in rows]
        needle = clean_text(q).lower() if q else ""
        if needle:
            notes = self.notes()
            repos = [r for r in repos if needle in r.slug.lower() or needle in r.description.lower()
                     or needle in notes.get(r.key, "").lower()]
        return repos

    # ---- notes, tags, collections (all local; every value is cleaned and bounded)

    def _is_fav(self, key: str) -> bool:
        return self._db.execute("SELECT 1 FROM favorites WHERE key = ?", (key,)).fetchone() is not None

    def notes(self) -> dict[str, str]:
        with self._lock:
            return dict(self._db.execute("SELECT key, note FROM fav_note").fetchall())

    def note(self, key: str) -> str:
        return self.notes().get(key, "")

    def set_note(self, key: str, text: str) -> bool:
        text = clean_text(text)[:MAX_NOTE]
        with self._lock:
            if not self._is_fav(key):
                return False
            if text:
                self._db.execute("INSERT INTO fav_note (key, note) VALUES (?, ?) "
                                 "ON CONFLICT(key) DO UPDATE SET note = excluded.note", (key, text))
            else:
                self._db.execute("DELETE FROM fav_note WHERE key = ?", (key,))
            self._db.commit()
        return True

    @staticmethod
    def clean_tag(value: str) -> str:
        tag = clean_text(value).lower()
        return tag if TAG_RE.fullmatch(tag) else ""

    def tags_by_key(self) -> dict[str, list[str]]:
        with self._lock:
            rows = self._db.execute("SELECT key, tag FROM fav_tag ORDER BY tag").fetchall()
        out: dict[str, list[str]] = {}
        for k, t in rows:
            out.setdefault(k, []).append(t)
        return out

    def tags(self, key: str) -> list[str]:
        return self.tags_by_key().get(key, [])

    def all_tags(self) -> list[tuple[str, int]]:
        with self._lock:
            return self._db.execute("SELECT tag, COUNT(*) FROM fav_tag GROUP BY tag ORDER BY tag").fetchall()

    def add_tag(self, key: str, tag: str) -> bool:
        tag = self.clean_tag(tag)
        with self._lock:
            if not tag or not self._is_fav(key):
                return False
            have = [r[0] for r in self._db.execute("SELECT tag FROM fav_tag WHERE key = ?", (key,)).fetchall()]
            if tag in have or len(have) >= MAX_TAGS:
                return False
            self._db.execute("INSERT INTO fav_tag (key, tag) VALUES (?, ?)", (key, tag))
            self._db.commit()
        return True

    def remove_tag(self, key: str, tag: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM fav_tag WHERE key = ? AND tag = ?", (key, self.clean_tag(tag)))
            self._db.commit()

    def collections(self) -> list[tuple[str, int]]:
        with self._lock:
            return self._db.execute(
                "SELECT c.name, COUNT(i.key) FROM collection c LEFT JOIN collection_item i ON i.name = c.name "
                "GROUP BY c.name ORDER BY c.name").fetchall()

    def collections_by_key(self) -> dict[str, list[str]]:
        with self._lock:
            rows = self._db.execute("SELECT key, name FROM collection_item ORDER BY name").fetchall()
        out: dict[str, list[str]] = {}
        for k, n in rows:
            out.setdefault(k, []).append(n)
        return out

    @staticmethod
    def clean_name(value: str) -> str:
        name = clean_text(value)
        return name if NAME_RE.fullmatch(name) else ""

    def create_collection(self, name: str) -> bool:
        name = self.clean_name(name)
        with self._lock:
            if not name or self._db.execute("SELECT COUNT(*) FROM collection").fetchone()[0] >= MAX_COLLECTIONS:
                return False
            cur = self._db.execute("INSERT OR IGNORE INTO collection (name, created) VALUES (?, ?)", (name, self._now()))
            self._db.commit()
            return cur.rowcount > 0

    def delete_collection(self, name: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM collection_item WHERE name = ?", (name,))
            self._db.execute("DELETE FROM collection WHERE name = ?", (name,))
            self._db.commit()

    def add_to_collection(self, name: str, key: str) -> bool:
        with self._lock:
            if not self._is_fav(key) or self._db.execute("SELECT 1 FROM collection WHERE name = ?", (name,)).fetchone() is None:
                return False
            if self._db.execute("SELECT COUNT(*) FROM collection_item WHERE name = ?", (name,)).fetchone()[0] >= MAX_PER_COLLECTION:
                return False
            cur = self._db.execute("INSERT OR IGNORE INTO collection_item (name, key) VALUES (?, ?)", (name, key))
            self._db.commit()
            return cur.rowcount > 0

    def remove_from_collection(self, name: str, key: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM collection_item WHERE name = ? AND key = ?", (name, key))
            self._db.commit()

    # ---- release tracking: which favorites have a release you have not looked at yet

    def record_release(self, key: str, tag: str, published_at: str | None) -> None:
        """Store the latest release seen. The first time, it counts as already seen (no flood of 'new')."""
        tag = clean_text(tag)[:100]
        with self._lock:
            if not tag or not self._is_fav(key):
                return
            row = self._db.execute("SELECT seen_tag FROM fav_release WHERE key = ?", (key,)).fetchone()
            seen = row[0] if row is not None else tag
            self._db.execute("INSERT INTO fav_release (key, latest_tag, published_at, checked_at, seen_tag) VALUES (?, ?, ?, ?, ?) "
                             "ON CONFLICT(key) DO UPDATE SET latest_tag = excluded.latest_tag, "
                             "published_at = excluded.published_at, checked_at = excluded.checked_at",
                             (key, tag, clean_text(published_at)[:40] if published_at else None, self._now(), seen))
            self._db.commit()

    def new_releases(self) -> list[tuple[Repo, str, str]]:
        """(favorite, latest release tag, published date) for releases newer than the last one marked seen."""
        with self._lock:
            rows = self._db.execute(
                "SELECT f.data, r.latest_tag, COALESCE(r.published_at, '') FROM fav_release r JOIN favorites f ON f.key = r.key "
                "WHERE r.seen_tag IS NULL OR r.seen_tag != r.latest_tag ORDER BY r.published_at DESC, f.key").fetchall()
        return [(Repo.from_dict(json.loads(d)), t, p) for d, t, p in rows]

    def mark_release_seen(self, key: str | None = None) -> None:
        """Mark one favorite's latest release (or all of them) as seen."""
        with self._lock:
            if key is None:
                self._db.execute("UPDATE fav_release SET seen_tag = latest_tag")
            else:
                self._db.execute("UPDATE fav_release SET seen_tag = latest_tag WHERE key = ?", (key,))
            self._db.commit()

    def stale(self, max_age: float) -> list[Repo]:
        cutoff = self._now() - max_age
        with self._lock:
            rows = self._db.execute("SELECT data FROM favorites WHERE refreshed_at < ?", (cutoff,)).fetchall()
        return [Repo.from_dict(json.loads(r[0])) for r in rows]
