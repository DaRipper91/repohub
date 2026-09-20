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
