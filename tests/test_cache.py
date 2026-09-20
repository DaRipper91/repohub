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
