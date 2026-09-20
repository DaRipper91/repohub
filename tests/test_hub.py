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
