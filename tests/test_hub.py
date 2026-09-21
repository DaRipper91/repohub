import pytest
from datetime import datetime, timezone

from helpers import FakeProvider, make_hub, mk
from repohub.core.browse import Shelf
from repohub.core.models import Asset, Release, SearchFilters
from repohub.core.providers.base import ProviderError, RateLimited


class Clock:
    t = 1_000_000.0

    def __call__(self):
        return self.t


async def test_search_is_cached_and_not_repeated():
    gh = FakeProvider("github", [mk("github", "a/a", 5)])
    hub = make_hub(gh)
    r1 = await hub.search("x")
    r2 = await hub.search("x")
    assert gh.calls == 1 and r1.repos == r2.repos


async def test_results_with_errors_are_not_cached():
    gh = FakeProvider("github", error=ProviderError("github", "boom"))
    hub = make_hub(gh)
    await hub.search("x")
    await hub.search("x")
    assert gh.calls == 2


async def test_all_hosts_failing_serves_stale_cache_with_flag():
    clock = Clock()
    gh = FakeProvider("github", [mk("github", "a/a", 5)])
    hub = make_hub(gh, clock=clock)
    await hub.search("x")
    clock.t += 10_000                                  # expire the cache
    gh.error = RateLimited("github", "rate limited")
    r = await hub.search("x")
    assert r.stale and [x.slug for x in r.repos] == ["a/a"] and r.errors == {"github": "rate limited"}


async def test_different_filters_use_different_cache_entries():
    gh = FakeProvider("github", [mk()])
    hub = make_hub(gh)
    await hub.search("x", SearchFilters(min_stars=1))
    await hub.search("x", SearchFilters(min_stars=2))
    assert gh.calls == 2


async def test_detail_combines_repo_readme_release_and_caches():
    rel = Release("v1", None, (Asset("a-arm64.tgz", 1, "u", "arm64"),))
    gh = FakeProvider("github", [mk("github", "o/r", 9)], readme="# hi", release=rel)
    hub = make_hub(gh)
    d = await hub.detail("github", "o/r")
    assert d.repo.stars == 9 and d.readme == "# hi" and d.release.has_arm64
    await hub.detail("github", "o/r")
    assert gh.calls == 1


async def test_detail_unknown_host_or_missing_repo_raises_provider_error():
    hub = make_hub(FakeProvider("github", [mk()]))
    for host, slug in (("nowhere", "o/r"),):
        try:
            await hub.detail(host, slug)
        except ProviderError:
            continue
        raise AssertionError("expected ProviderError")


async def test_detail_survives_readme_failure():
    class Boom(FakeProvider):
        async def readme(self, slug):
            raise ProviderError("github", "boom")

    hub = make_hub(Boom("github", [mk("github", "o/r")]))
    d = await hub.detail("github", "o/r")
    assert d.readme is None


async def test_shelf_search_uses_shelf_filters():
    gh = FakeProvider("github", [mk("github", "a/a", 500)])
    hub = make_hub(gh)
    r = await hub.shelf(Shelf(name="T", topic="tui", min_stars=100, days=365))
    assert [x.slug for x in r.repos] == ["a/a"]


async def test_refresh_favorites_updates_only_stale_entries():
    clock = Clock()
    gh = FakeProvider("github", [mk("github", "a/a", 1)])
    hub = make_hub(gh, clock=clock)
    hub.favorites.add(mk("github", "a/a", 1))
    gh.repos = [mk("github", "a/a", 77)]
    await hub.refresh_favorites(max_age=86400)
    assert hub.favorites.list()[0].stars == 1          # fresh: untouched
    clock.t += 90_000
    await hub.refresh_favorites(max_age=86400)
    assert hub.favorites.list()[0].stars == 77


class FlakyReadme(FakeProvider):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.readme_calls = 0

    async def readme(self, slug):
        self.readme_calls += 1
        if self.readme_calls == 1:
            raise RateLimited("github", "rate limited")
        return "# back"


async def test_failed_readme_is_cached_only_briefly():
    clock = Clock()
    gh = FlakyReadme("github", [mk("github", "o/r")])
    hub = make_hub(gh, clock=clock)
    assert (await hub.detail("github", "o/r")).readme is None
    clock.t += 61
    assert (await hub.detail("github", "o/r")).readme == "# back"
    assert gh.readme_calls == 2


async def test_legitimately_missing_readme_is_cached_for_full_ttl():
    clock = Clock()
    gh = FakeProvider("github", [mk("github", "o/r")], readme=None)
    hub = make_hub(gh, clock=clock)
    await hub.detail("github", "o/r")
    clock.t += 61
    await hub.detail("github", "o/r")
    assert gh.calls == 1


async def test_failed_release_degrades_with_short_ttl():
    class BadRelease(FakeProvider):
        async def latest_release(self, slug):
            raise ProviderError("github", "boom")

    clock = Clock()
    gh = BadRelease("github", [mk("github", "o/r")])
    hub = make_hub(gh, clock=clock)
    d = await hub.detail("github", "o/r")
    assert d.release is None
    clock.t += 61
    await hub.detail("github", "o/r")
    assert gh.calls == 2


async def test_cancelled_error_in_readme_propagates_and_is_not_cached():
    import asyncio

    class Cancel(FakeProvider):
        async def readme(self, slug):
            raise asyncio.CancelledError()

    gh = Cancel("github", [mk("github", "o/r")])
    hub = make_hub(gh)
    try:
        await hub.detail("github", "o/r")
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("expected CancelledError")
    assert hub.cache.get("detail:github:o/r", allow_stale=True) is None


async def test_programming_error_in_readme_propagates():
    import pytest

    class Bug(FakeProvider):
        async def readme(self, slug):
            raise ValueError("bug")

    hub = make_hub(Bug("github", [mk("github", "o/r")]))
    with pytest.raises(ValueError):
        await hub.detail("github", "o/r")


async def test_refresh_favorites_is_concurrency_bounded():
    import asyncio

    class Counting(FakeProvider):
        active = peak = 0

        async def repo(self, slug):
            type(self).active += 1
            type(self).peak = max(type(self).peak, type(self).active)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            type(self).active -= 1
            return mk("github", slug, 5)

    clock = Clock()
    gh = Counting("github")
    hub = make_hub(gh, clock=clock)
    for i in range(30):
        hub.favorites.add(mk("github", f"o/r{i}", 1))
    clock.t += 90_000
    await hub.refresh_favorites(max_age=86400)
    assert Counting.peak <= 8
    assert all(r.stars == 5 for r in hub.favorites.list()) and len(hub.favorites.list()) == 30


async def test_detail_caps_huge_readme():
    from repohub.core.hub import MAX_README_CHARS
    gh = FakeProvider("github", [mk("github", "o/r")], readme="x" * 250_000)
    d = await make_hub(gh).detail("github", "o/r")
    assert d.readme.startswith("x" * MAX_README_CHARS) and d.readme.endswith("\n\n_[README truncated]_")
    assert len(d.readme) < 200_100


async def test_detail_leaves_short_readme_alone():
    gh = FakeProvider("github", [mk("github", "o/r")], readme="short")
    assert (await make_hub(gh).detail("github", "o/r")).readme == "short"


async def test_detail_serves_stale_on_any_provider_error_except_not_found():
    from repohub.core.providers.base import NotFound

    clock = Clock()
    gh = FakeProvider("github", [mk("github", "o/r")])
    hub = make_hub(gh, clock=clock)
    await hub.detail("github", "o/r")
    clock.t += 7200
    gh.detail_error = ProviderError("github", "network error")
    assert (await hub.detail("github", "o/r")).repo.slug == "o/r"
    gh.detail_error = NotFound("github", "repository not found")
    with pytest.raises(NotFound):
        await hub.detail("github", "o/r")


async def test_failing_favorite_is_not_refetched_within_max_age():
    from repohub.core.providers.base import NotFound

    clock = Clock()
    gh = FakeProvider("github", [mk("github", "o/r")], detail_error=NotFound("github", "repository not found"))
    hub = make_hub(gh, clock=clock)
    hub.favorites.add(mk("github", "o/r"))
    clock.t += 100_000
    await hub.refresh_favorites()
    assert gh.calls == 1
    clock.t += 1000
    await hub.refresh_favorites()
    assert gh.calls == 1
    clock.t += 100_000
    await hub.refresh_favorites()
    assert gh.calls == 2
    assert hub.favorites.list() == [mk("github", "o/r")]


# ---- curated shelves (Task 8) ----

import asyncio  # noqa: E402

from repohub.core.browse import ShelfEntry, Snapshot  # noqa: E402
from repohub.core.hub import CuratedPage, repo_from_snapshot  # noqa: E402


def curated(n=30, host="github", as_of="2026-09-01", snap=True):
    entries = tuple(
        ShelfEntry(host, f"o/r{i}", note=f"note{i}",
                   snapshot=Snapshot(f"snap{i}", 100 + i, "Go", "MIT", "2026-08-01") if snap else None)
        for i in range(n))
    return Shelf(name="C", repos=entries, as_of=as_of)


def live_provider(host="github", n=30, stars=999):
    return FakeProvider(host, [mk(host, f"o/r{i}", stars, description="live") for i in range(n)])


def test_repo_from_snapshot_builds_canonical_repo():
    e = ShelfEntry("gitlab", "g/p", "n", Snapshot("desc", 7, "Rust", "GPL", "2026-01-02"))
    r = repo_from_snapshot(e, "2026-09-01")
    assert (r.host, r.slug, r.url) == ("gitlab", "g/p", "https://gitlab.com/g/p")
    assert (r.description, r.stars, r.language, r.license, r.pushed_at) == ("desc", 7, "Rust", "GPL", "2026-01-02")
    assert r.topics == () and r.archived is False and r.forks == 0 and r.homepage == "" and r.fork is False
    bare = repo_from_snapshot(ShelfEntry("github", "a/b"), None)
    assert bare.url == "https://github.com/a/b"
    assert (bare.description, bare.stars, bare.language, bare.license, bare.pushed_at) == ("", 0, "", "", "")


async def test_curated_pages_slice_and_clamp():
    hub = make_hub(live_provider())
    shelf = curated(30)
    p1 = await hub.curated_page(shelf, 0, 12)
    assert isinstance(p1, CuratedPage) and len(p1.items) == 12 and p1.total == 30
    assert (p1.offset, p1.limit) == (0, 12)
    assert [i.repo.slug for i in p1.items] == [f"o/r{i}" for i in range(12)]
    assert len((await hub.curated_page(shelf, 24, 12)).items) == 6
    assert (await hub.curated_page(shelf, 100, 12)).items == []
    p0 = await hub.curated_page(shelf, -5, 0)
    assert p0.offset == 0 and p0.limit == 1 and len(p0.items) == 1
    big = await hub.curated_page(shelf, 0, 1000)
    assert big.limit == 50 and len(big.items) == 30


async def test_snapshot_only_when_every_refresh_fails():
    gh = FakeProvider("github", detail_error=ProviderError("github", "down"))
    hub = make_hub(gh)
    page = await hub.curated_page(curated(5), 0, 12)
    assert page.errors == {"github": "down"}
    for i, item in enumerate(page.items):
        assert item.live is False and item.note == f"note{i}" and item.as_of == "2026-09-01"
        assert item.repo.stars == 100 + i and item.repo.description == f"snap{i}"


async def test_successful_refresh_replaces_repo_but_keeps_entry_note():
    hub = make_hub(live_provider(n=3))
    page = await hub.curated_page(curated(3), 0, 12)
    assert page.errors == {}
    for i, item in enumerate(page.items):
        assert item.live is True and item.repo.stars == 999 and item.repo.description == "live"
        assert item.note == f"note{i}"
        assert item.repo.topics == ()  # provider repo, not merged with snapshot


async def test_partial_failure_mixes_live_and_snapshot():
    class Flaky(FakeProvider):
        async def repo(self, slug):
            if slug in ("o/r1", "o/r2"):
                raise ProviderError("github", f"bad {slug}")
            return await super().repo(slug)

    hub = make_hub(Flaky("github", [mk("github", f"o/r{i}", 999) for i in range(4)]))
    page = await hub.curated_page(curated(4), 0, 12)
    assert [i.live for i in page.items] == [True, False, False, True]
    assert page.errors == {"github": "bad o/r1"}  # one per host, first (in entry order) wins
    assert page.items[1].repo.stars == 101


async def test_refreshed_slug_case_difference_maps_to_right_entry():
    entries = (ShelfEntry("github", "Foo/Bar", "nb", Snapshot("s", 1)),
               ShelfEntry("github", "Baz/Qux", "nq", Snapshot("s", 2)))
    gh = FakeProvider("github", [mk("github", "foo/bar", 50), mk("github", "baz/qux", 60)])
    page = await make_hub(gh).curated_page(Shelf(name="C", repos=entries), 0, 12)
    assert [(i.repo.stars, i.note, i.live) for i in page.items] == [(50, "nb", True), (60, "nq", True)]


async def test_unknown_host_keeps_snapshot_and_reports_host_not_configured():
    page = await make_hub().curated_page(curated(2, host="gitlab"), 0, 12)
    assert page.errors == {"gitlab": "host not configured"} and not any(i.live for i in page.items)


async def test_curated_refresh_concurrency_bounded_and_full():
    class Counting(FakeProvider):
        active = peak = 0

        async def repo(self, slug):
            type(self).active += 1
            type(self).peak = max(type(self).peak, type(self).active)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            type(self).active -= 1
            return await super().repo(slug)

    gh = Counting("github", [mk("github", f"o/r{i}", 5) for i in range(30)])
    page = await make_hub(gh).curated_page(curated(30), 0, 30)
    assert 1 < Counting.peak <= 8 and len(page.items) == 30 and all(i.live for i in page.items)


async def test_repo_summary_caches_for_ttl_and_refetches_after():
    from repohub.core.hub import DETAIL_TTL
    clock = Clock()
    gh = FakeProvider("github", [mk("github", "O/R", 5)])
    hub = make_hub(gh, clock=clock)
    a = await hub.repo_summary("github", "O/R")
    b = await hub.repo_summary("github", "o/r")
    assert gh.calls == 1 and a == b
    assert hub.cache.get("repo:github@github.com:o/r") == a.to_dict()
    clock.t += DETAIL_TTL + 1
    await hub.repo_summary("github", "o/r")
    assert gh.calls == 2


async def test_repo_summary_unconfigured_host_and_provider_errors_propagate():
    hub = make_hub(FakeProvider("github", detail_error=ProviderError("github", "boom")))
    with pytest.raises(ProviderError, match="host not configured") as ei:
        await hub.repo_summary("gitlab", "a/b")
    assert ei.value.host == "gitlab"
    with pytest.raises(ProviderError, match="boom"):
        await hub.repo_summary("github", "a/b")


async def test_runtime_error_in_refresh_keeps_snapshot_as_unexpected_error():
    gh = FakeProvider("github", detail_error=RuntimeError("secret internals"))
    page = await make_hub(gh).curated_page(curated(3), 0, 12)
    assert page.errors == {"github": "unexpected error"}
    assert not any(i.live for i in page.items) and page.items[0].repo.description == "snap0"


async def test_cancelled_error_from_provider_propagates_and_cleans_up():
    class Cancelling(FakeProvider):
        started = 0
        finished = 0

        async def repo(self, slug):
            type(self).started += 1
            if slug == "o/r0":
                raise asyncio.CancelledError
            try:
                await asyncio.Event().wait()
            finally:
                type(self).finished += 1

    hub = make_hub(Cancelling("github"))
    with pytest.raises(asyncio.CancelledError):
        await hub.curated_page(curated(5), 0, 12)
    await asyncio.sleep(0)
    assert Cancelling.started == Cancelling.finished + 1  # siblings were cancelled, not leaked


async def test_outer_cancellation_cancels_pending_refreshes():
    gate = asyncio.Event()

    class Slow(FakeProvider):
        cancelled = 0

        async def repo(self, slug):
            try:
                await gate.wait()
            except asyncio.CancelledError:
                type(self).cancelled += 1
                raise

    hub = make_hub(Slow("github"))
    task = asyncio.ensure_future(hub.curated_page(curated(20), 0, 12))
    for _ in range(5):
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)
    assert Slow.cancelled == 8  # the 8 in flight; the rest never started


async def test_curated_page_does_not_touch_favorites():
    hub = make_hub(live_provider(n=3))
    await hub.curated_page(curated(3), 0, 12)
    assert hub.favorites.list() == []


async def test_hub_shelf_curated_returns_first_page_repos():
    hub = make_hub(live_provider(n=30))
    res = await hub.shelf(curated(30))
    assert len(res.repos) == 12 and res.errors == {} and all(r.stars == 999 for r in res.repos)
    bad = make_hub(FakeProvider("github", detail_error=ProviderError("github", "down")))
    res = await bad.shelf(curated(30))
    assert res.errors == {"github": "down"} and res.repos[0].description == "snap0"


async def test_curated_page_refresh_false_never_calls_provider_and_uses_snapshots():
    gh = live_provider(n=5)
    hub = make_hub(gh)
    page = await hub.curated_page(curated(5), 0, 12, refresh=False)
    assert gh.calls == 0 and page.errors == {} and page.total == 5
    for i, item in enumerate(page.items):
        assert item.live is False and item.as_of == "2026-09-01" and item.note == f"note{i}"
        assert item.repo.stars == 100 + i and item.repo.description == f"snap{i}"


async def test_curated_page_refresh_false_ignores_cache_and_unknown_hosts():
    hub = make_hub()  # no providers at all: still no error
    page = await hub.curated_page(curated(3, host="gitlab"), 3, 12, refresh=False)
    assert page.errors == {} and page.items == []
    page = await hub.curated_page(curated(3, host="gitlab"), 0, 2, refresh=False)
    assert len(page.items) == 2 and page.errors == {} and not any(i.live for i in page.items)


# ---- rate-limit memory for curated refreshes

class LimitedProvider(FakeProvider):
    def __init__(self, host, reset_at=None):
        super().__init__(host)
        self.reset_at = reset_at

    async def repo(self, slug):
        self.calls += 1
        await asyncio.sleep(0)
        raise RateLimited(self.host, "rate limited", self.reset_at)


async def test_rate_limit_stops_further_calls_within_first_page():
    from repohub.core.hub import REFRESH_CONCURRENCY
    gh = LimitedProvider("github")
    hub = make_hub(gh, clock=Clock())
    page = await hub.curated_page(curated(25), 0, 25)
    assert gh.calls <= REFRESH_CONCURRENCY
    assert page.errors == {"github": "rate limited"}
    assert all(not i.live for i in page.items) and page.items[20].repo.stars == 120


async def test_second_view_inside_window_makes_no_calls_and_after_window_calls_again():
    clock = Clock()
    gh = LimitedProvider("github")
    hub = make_hub(gh, clock=clock)
    await hub.curated_page(curated(5), 0, 12)
    n = gh.calls
    page = await hub.curated_page(curated(5), 0, 12)
    assert gh.calls == n
    assert page.errors == {"github": "rate limited"} and page.items[2].repo.stars == 102
    clock.t += 61
    await hub.curated_page(curated(5), 0, 12)
    assert gh.calls > n


async def test_other_host_still_refreshed_while_one_is_limited():
    gh = LimitedProvider("github")
    gl = live_provider("gitlab", n=1)
    hub = make_hub(gh, gl, clock=Clock())
    entries = (ShelfEntry("github", "o/a"), ShelfEntry("gitlab", "o/r0"))
    page = await hub.curated_page(Shelf(name="M", repos=entries), 0, 12)
    assert [i.live for i in page.items] == [False, True]
    assert set(page.errors) == {"github"}


@pytest.mark.parametrize("reset_delta,expected", [
    (None, 60), (-500, 60), (10, 60), (600, 600), (10**9, 3600)])
async def test_rate_limit_window_is_clamped(reset_delta, expected):
    clock = Clock()
    reset = None if reset_delta is None else int(clock.t) + reset_delta
    gh = LimitedProvider("github", reset_at=reset)
    hub = make_hub(gh, clock=clock)
    await hub.curated_page(curated(1), 0, 12)
    assert hub._limited_until["github"][0] == clock.t + expected


async def test_mixed_curated_page_only_calls_configured_provider():
    gh = FakeProvider("github", [mk("github", "o/r0", 999), mk("github", "o/r1", 999)])
    entries = (ShelfEntry("github", "o/r0", "n0", Snapshot("s0", 1)),
               ShelfEntry("gone", "o/x", "nx", Snapshot("sx", 7)),
               ShelfEntry("gone", "o/y", "ny", Snapshot("sy", 8)),
               ShelfEntry("github", "o/r1", "n1", Snapshot("s1", 2)))
    page = await make_hub(gh).curated_page(Shelf(name="M", repos=entries, as_of="2026-09-01"), 0, 12)
    assert gh.calls == 2
    assert [i.live for i in page.items] == [True, False, False, True]
    assert page.items[1].repo.stars == 7 and page.items[1].repo.description == "sx"
    assert page.errors == {"gone": "host not configured"}  # one message per host id


async def test_unconfigured_host_does_not_touch_rate_limit_memory():
    hub = make_hub(FakeProvider("github", [mk("github", "o/r0", 5)]))
    await hub.curated_page(curated(3, host="gone"), 0, 12)
    assert hub._limited_until == {}
    assert hub._limit_message("gone") is None


async def test_repo_summary_and_detail_for_registered_host_without_provider():
    from repohub.core import hosts
    assert "codeberg" in hosts.registry()
    hub = make_hub(FakeProvider("github"))
    for call in (hub.repo_summary, hub.detail):
        with pytest.raises(ProviderError, match="^host not configured$") as ei:
            await call("codeberg", "o/r")
        assert ei.value.host == "codeberg"


async def test_removed_host_keeps_favorites_and_refresh_skips_them():
    from repohub.core import hosts
    gh = FakeProvider("github", [mk("github", "o/r", 5)])
    hub = make_hub(gh)
    hub.favorites.add(mk("gone", "o/r"))
    hub.favorites.add(mk("github", "o/r"))
    reg = hosts.registry()
    hosts.set_registry(hosts.HostRegistry([s for s in reg.specs if s.id != "gitlab"]))
    await hub.refresh_favorites(max_age=-1)
    assert {r.key for r in hub.favorites.list()} == {"gone:o/r", "github:o/r"}
    assert gh.calls == 1  # only the configured host was contacted


def test_snapshot_url_uses_the_registry_domain():
    from repohub.core.hosts import BUILTIN_HOSTS, HostRegistry, HostSpec, set_registry
    snap = Snapshot("d", 1, "Go", "MIT", "2026-01-01")
    assert repo_from_snapshot(ShelfEntry("codeberg", "o/r", "", snap), None).url == "https://codeberg.org/o/r"
    assert repo_from_snapshot(ShelfEntry("github", "o/r", "", snap), None).url == "https://github.com/o/r"
    assert repo_from_snapshot(ShelfEntry("gitlab", "g/s/r", "", snap), None).url == "https://gitlab.com/g/s/r"
    extra = HostSpec("myforge", "forgejo", "myforge", "git.example.org", "https://git.example.org/api/v1",
                     ("REPOHUB_MYFORGE_TOKEN",))
    set_registry(HostRegistry(BUILTIN_HOSTS + (extra,)))
    assert repo_from_snapshot(ShelfEntry("myforge", "o/r", "", snap), None).url == "https://git.example.org/o/r"


def test_snapshot_url_is_empty_for_an_unconfigured_host():
    r = repo_from_snapshot(ShelfEntry("nowhere", "o/r", "", None), None)
    assert r.url == "" and r.host == "nowhere"


# ---- per-host deadline, partial caching, cache keys --------------------------------------------

class Hang(FakeProvider):
    """Every call waits on an event that is never set."""

    def __init__(self, host):
        super().__init__(host, [mk(host, "o/r0", 5)])
        self.never = asyncio.Event()

    async def search(self, query, filters, per_page=30):
        await self.never.wait()

    async def repo(self, slug):
        await self.never.wait()

    async def readme(self, slug):
        await self.never.wait()

    async def latest_release(self, slug):
        await self.never.wait()


@pytest.fixture
def fast_deadline(monkeypatch):
    import repohub.core.search as search_mod
    monkeypatch.setattr(search_mod, "HOST_DEADLINE", 0.05)


def test_default_deadline_is_twenty_seconds():
    import repohub.core.search as search_mod
    assert search_mod.HOST_DEADLINE == 20.0


async def test_search_times_out_one_host_and_keeps_the_others(fast_deadline):
    import time
    hub = make_hub(Hang("github"), FakeProvider("gitlab", [mk("gitlab", "g/p", 3)]))
    t0 = time.monotonic()
    r = await hub.search("x")
    assert time.monotonic() - t0 < 5
    assert r.errors == {"github": "timed out"} and [x.slug for x in r.repos] == ["g/p"]


async def test_repo_summary_times_out(fast_deadline):
    hub = make_hub(Hang("github"))
    with pytest.raises(ProviderError, match="timed out"):
        await hub.repo_summary("github", "o/r0")


async def test_curated_page_keeps_snapshot_on_timeout(fast_deadline):
    hub = make_hub(Hang("github"), live_provider("gitlab", n=3))
    shelf = Shelf(name="C", as_of="2026-09-01", repos=(
        ShelfEntry("github", "o/r0", note="n", snapshot=Snapshot("snap", 5, "Go", "MIT", "2026-08-01")),
        ShelfEntry("gitlab", "o/r1", note="n")))
    page = await hub.curated_page(shelf, 0, 12)
    assert page.errors == {"github": "timed out"}
    assert [i.live for i in page.items] == [False, True] and page.items[0].repo.description == "snap"


async def test_detail_repo_timeout_raises_and_readme_timeout_degrades(fast_deadline):
    with pytest.raises(ProviderError, match="timed out"):
        await make_hub(Hang("github")).detail("github", "o/r0")

    class SlowReadme(FakeProvider):
        async def readme(self, slug):
            await asyncio.Event().wait()

        async def latest_release(self, slug):
            await asyncio.Event().wait()

    d = await make_hub(SlowReadme("github", [mk("github", "o/r0")])).detail("github", "o/r0")
    assert d.repo.slug == "o/r0" and d.readme is None and d.release is None


async def test_slow_but_in_time_provider_is_unaffected(monkeypatch):
    import repohub.core.search as search_mod
    monkeypatch.setattr(search_mod, "HOST_DEADLINE", 2.0)

    class Slowish(FakeProvider):
        async def search(self, query, filters, per_page=30):
            await asyncio.sleep(0.05)
            return await super().search(query, filters, per_page)

    r = await make_hub(Slowish("github", [mk("github", "o/r0")])).search("x")
    assert r.errors == {} and len(r.repos) == 1


async def test_caller_cancellation_is_not_swallowed():
    hub = make_hub(Hang("github"))
    task = asyncio.ensure_future(hub.search("x"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_partial_results_are_cached_for_sixty_seconds():
    clock = Clock()
    ok = FakeProvider("gitlab", [mk("gitlab", "g/p", 3)])
    bad = FakeProvider("github", error=ProviderError("github", "boom"))
    hub = make_hub(bad, ok, clock=clock)
    for _ in range(3):
        r = await hub.search("x")
    assert ok.calls == 1 and bad.calls == 1
    assert r.errors == {"github": "boom"} and [x.slug for x in r.repos] == ["g/p"]
    clock.t += 61
    await hub.search("x")
    assert ok.calls == 2


async def test_all_hosts_failing_is_still_not_cached():
    a = FakeProvider("github", error=ProviderError("github", "boom"))
    b = FakeProvider("gitlab", error=ProviderError("gitlab", "boom"))
    hub = make_hub(a, b)
    await hub.search("x")
    await hub.search("x")
    assert a.calls == 2 and b.calls == 2


async def test_repointing_a_host_misses_the_cache():
    from repohub.core.hosts import BUILTIN_HOSTS, HostRegistry, HostSpec, set_registry

    def reg(domain):
        return HostRegistry(BUILTIN_HOSTS + (HostSpec("myforge", "forgejo", "m", domain, f"https://{domain}/api/v1"),))

    p = FakeProvider("myforge", [mk("myforge", "o/r")])
    hub = make_hub(p)
    hub.providers["myforge"] = p
    set_registry(reg("one.example.org"))
    from repohub.core.models import SearchFilters as F
    f = F(hosts=("myforge",))
    await hub.search("x", f)
    await hub.repo_summary("myforge", "o/r")
    await hub.detail("myforge", "o/r")
    calls = p.calls
    await hub.search("x", f)
    await hub.repo_summary("myforge", "o/r")
    await hub.detail("myforge", "o/r")
    assert p.calls == calls                      # unchanged registry: all cache hits
    set_registry(reg("two.example.org"))
    await hub.search("x", f)
    await hub.repo_summary("myforge", "o/r")
    await hub.detail("myforge", "o/r")
    assert p.calls == calls + 3                  # repointed: three misses
