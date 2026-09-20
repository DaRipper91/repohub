from helpers import mk
from repohub.core.store import Favorites


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


def test_add_list_remove():
    f = Favorites()
    r = mk("github", "O/R", 5)
    f.add(r)
    assert f.is_favorite(r.key) and f.list() == [r]
    f.remove(r.key)
    assert not f.is_favorite(r.key) and f.list() == []


def test_add_twice_keeps_one_row():
    f = Favorites()
    f.add(mk())
    f.add(mk(stars=99))
    assert len(f.list()) == 1 and f.list()[0].stars == 99


def test_stale_lists_entries_not_refreshed_recently():
    clock = Clock()
    f = Favorites(now=clock)
    f.add(mk("github", "a/a"))
    clock.t += 100
    assert f.stale(max_age=50) == [mk("github", "a/a")]
    assert f.stale(max_age=500) == []


def test_update_marks_fresh_and_persists(tmp_path):
    clock = Clock()
    p = str(tmp_path / "f.db")
    f = Favorites(p, now=clock)
    f.add(mk("github", "a/a", 1))
    clock.t += 100
    f.update(mk("github", "a/a", 7))
    assert f.stale(max_age=50) == []
    assert Favorites(p).list()[0].stars == 7


def test_update_does_not_readd_a_removed_favorite():
    f = Favorites()
    r = mk("github", "a/a", 1)
    f.add(r)
    f.remove(r.key)
    f.update(r)
    assert not f.is_favorite(r.key)
    assert f.list() == []


def test_update_refreshes_data_but_keeps_added_at_and_order():
    clock = Clock()
    f = Favorites(now=clock)
    a = mk("github", "a/a", 1)
    b = mk("github", "b/b", 2)
    f.add(a)
    clock.t = 2000.0
    f.add(b)
    clock.t = 3000.0
    f.update(mk("github", "a/a", 50))
    assert [r.stars for r in f.list()] == [2, 50]
    assert [r.key for r in f.list()] == [b.key, a.key]
    clock.t = 3100.0
    assert [r.key for r in f.stale(max_age=500)] == [b.key]


def test_mark_checked_refreshes_timestamp_without_changing_data():
    clock = Clock()
    f = Favorites(now=clock)
    f.add(mk("github", "a/a", 5))
    clock.t += 1000
    f.mark_checked(mk("github", "a/a").key)
    assert f.stale(max_age=500) == [] and f.list() == [mk("github", "a/a", 5)]


def test_mark_checked_ignores_unknown_key():
    f = Favorites()
    f.mark_checked("github:nope/x")
    assert f.list() == []
