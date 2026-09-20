from datetime import datetime, timezone

from helpers import FakeProvider, mk
from repohub.core.models import SearchFilters
from repohub.core.providers.base import ProviderError
from repohub.core.search import SearchResult, search_all


async def run(providers, filters=SearchFilters(), now=None):
    return await search_all({p.host: p for p in providers}, "q", filters, now=now)


async def test_merges_and_sorts_by_stars():
    gh = FakeProvider("github", [mk("github", "a/a", 5), mk("github", "b/b", 50)])
    gl = FakeProvider("gitlab", [mk("gitlab", "c/c", 20)])
    r = await run([gh, gl])
    assert [x.slug for x in r.repos] == ["b/b", "c/c", "a/a"] and r.errors == {}


async def test_dedupes_same_slug_keeping_higher_stars_and_preferring_github_on_tie():
    gh = FakeProvider("github", [mk("github", "x/y", 10), mk("github", "t/t", 7)])
    gl = FakeProvider("gitlab", [mk("gitlab", "X/Y", 10), mk("gitlab", "t/t", 9)])
    r = await run([gh, gl])
    by = {x.slug.lower(): x for x in r.repos}
    assert by["x/y"].host == "github" and by["t/t"].host == "gitlab" and len(r.repos) == 2


async def test_one_host_failing_keeps_other_results_and_reports_error():
    gh = FakeProvider("github", error=ProviderError("github", "rate limited"))
    gl = FakeProvider("gitlab", [mk("gitlab", "c/c", 3)])
    r = await run([gh, gl])
    assert [x.slug for x in r.repos] == ["c/c"] and r.errors == {"github": "rate limited"}


async def test_unexpected_exception_does_not_leak_details():
    gh = FakeProvider("github", error=RuntimeError("secret token abc"))
    r = await run([gh])
    assert r.errors == {"github": "unexpected error"}


async def test_client_side_filters():
    gl = FakeProvider("gitlab", [
        mk("gitlab", "a/a", 5), mk("gitlab", "b/b", 500), mk("gitlab", "c/c", 900, archived=True),
        mk("gitlab", "d/d", 800, pushed_at="2020-01-01T00:00:00Z"),
    ])
    now = datetime(2026, 9, 20, tzinfo=timezone.utc)
    r = await run([gl], SearchFilters(min_stars=100, updated_within_days=30), now=now)
    assert [x.slug for x in r.repos] == ["b/b"]
    r2 = await run([gl], SearchFilters(min_stars=100, include_archived=True), now=now)
    assert "c/c" in [x.slug for x in r2.repos]


async def test_only_selected_hosts_are_queried():
    gh, gl = FakeProvider("github", [mk()]), FakeProvider("gitlab", [mk("gitlab", "z/z")])
    r = await run([gh, gl], SearchFilters(hosts=("gitlab",)))
    assert gh.calls == 0 and gl.calls == 1 and [x.host for x in r.repos] == ["gitlab"]


def test_result_roundtrip():
    r = SearchResult([mk()], {"gitlab": "x"}, stale=True)
    assert SearchResult.from_dict(r.to_dict()) == r
