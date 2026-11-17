from repohub.core.cache import Cache


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_set_and_get_roundtrip():
    c = Cache(now=Clock())
    c.set("k", {"a": [1, 2]}, ttl=60)
    assert c.get("k") == {"a": [1, 2]}


def test_missing_key_is_none():
    assert Cache(now=Clock()).get("nope") is None


def test_expired_entries_are_hidden_unless_stale_allowed():
    clock = Clock()
    c = Cache(now=clock)
    c.set("k", "v", ttl=10)
    clock.t += 11
    assert c.get("k") is None
    assert c.get("k", allow_stale=True) == "v"


def test_overwrite_replaces_value_and_ttl():
    clock = Clock()
    c = Cache(now=clock)
    c.set("k", "old", ttl=1)
    c.set("k", "new", ttl=100)
    clock.t += 50
    assert c.get("k") == "new"


def test_persists_to_file(tmp_path):
    p = tmp_path / "c.db"
    Cache(str(p)).set("k", 1, ttl=100)
    assert Cache(str(p)).get("k") == 1


def test_purge_keeps_stale_within_retention_and_drops_older():
    clock = Clock()
    c = Cache(now=clock, retention=100)
    c.set("old", 1, ttl=10)
    c.set("recent", 2, ttl=10)
    clock.t += 105  # "old" and "recent" both expired 95s ago -> inside retention
    assert c.purge() == 0
    assert c.get("old", allow_stale=True) == 1
    clock.t += 10  # expired 105s ago -> past retention
    assert c.purge() == 2
    assert c.get("old", allow_stale=True) is None


def test_purge_does_not_touch_live_entries():
    clock = Clock()
    c = Cache(now=clock, retention=0)
    c.set("live", "v", ttl=1000)
    clock.t += 10
    assert c.purge() == 0
    assert c.get("live") == "v"


def test_open_purges_expired_rows_in_existing_file(tmp_path):
    p = str(tmp_path / "c.db")
    clock = Clock()
    Cache(p, now=clock, retention=50).set("k", 1, ttl=10)
    clock.t += 100
    assert Cache(p, now=clock, retention=50).get("k", allow_stale=True) is None


def test_set_purges_opportunistically_after_interval():
    clock = Clock()
    c = Cache(now=clock, retention=10)
    c.set("a", 1, ttl=1)
    clock.t += 4000  # past retention and past PURGE_INTERVAL
    c.set("b", 2, ttl=100)
    assert c.get("a", allow_stale=True) is None
    assert c.get("b") == 2


def test_purge_leaves_other_tables_alone(tmp_path):
    import sqlite3
    p = str(tmp_path / "c.db")
    clock = Clock()
    c = Cache(p, now=clock, retention=0)
    c._db.execute("CREATE TABLE other (x INTEGER)")
    c._db.execute("INSERT INTO other VALUES (1)")
    c._db.commit()
    c.set("k", 1, ttl=1)
    clock.t += 5
    c.purge()
    assert sqlite3.connect(p).execute("SELECT COUNT(*) FROM other").fetchone()[0] == 1
