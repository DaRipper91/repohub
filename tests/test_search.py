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


async def test_huge_updated_within_days_does_not_raise():
    gh = FakeProvider("github", [mk("github", "a/a", 5)])
    r = await run([gh], SearchFilters(updated_within_days=10**20))
    assert r.errors == {}


async def test_sort_updated_orders_by_pushed_at_desc_ties_github_first_empty_last():
    gh = FakeProvider("github", [
        mk("github", "a/old", 1, pushed_at="2026-01-01T00:00:00Z"),
        mk("github", "b/none", 99, pushed_at=""),
        mk("github", "c/tie", 1, pushed_at="2026-05-01T00:00:00Z"),
    ])
    gl = FakeProvider("gitlab", [
        mk("gitlab", "a/tie", 500, pushed_at="2026-05-01T00:00:00Z"),
        mk("gitlab", "d/new", 2, pushed_at="2026-09-01T00:00:00Z"),
    ])
    r = await run([gh, gl], SearchFilters(sort="updated"))
    assert [x.slug for x in r.repos] == ["d/new", "c/tie", "a/tie", "a/old", "b/none"]


async def test_sort_forks_interleaves_hosts():
    gh = FakeProvider("github", [mk("github", "a/a", 1, forks=5), mk("github", "b/b", 1, forks=1)])
    gl = FakeProvider("gitlab", [mk("gitlab", "c/c", 1, forks=3), mk("gitlab", "d/d", 1, forks=9)])
    r = await run([gh, gl], SearchFilters(sort="forks"))
    assert [x.slug for x in r.repos] == ["d/d", "a/a", "c/c", "b/b"]


async def test_hide_forks_removes_forks_from_both_hosts():
    gh = FakeProvider("github", [mk("github", "a/a", 5, fork=True), mk("github", "b/b", 4)])
    gl = FakeProvider("gitlab", [mk("gitlab", "c/c", 3, fork=True), mk("gitlab", "d/d", 2)])
    r = await run([gh, gl], SearchFilters(hide_forks=True))
    assert [x.slug for x in r.repos] == ["b/b", "d/d"]
    r2 = await run([gh, gl])
    assert len(r2.repos) == 4


def test_ordered_is_stable_github_first_then_slug():
    from repohub.core.search import _ordered
    repos = [mk("gitlab", "a/a", 5), mk("github", "z/z", 5), mk("github", "B/b", 5), mk("gitlab", "0/0", 9)]
    assert [x.slug for x in _ordered(repos, "stars")] == ["0/0", "B/b", "z/z", "a/a"]
    assert [x.slug for x in _ordered(repos, "bogus")] == ["0/0", "B/b", "z/z", "a/a"]


async def test_three_host_tie_break_order_github_gitlab_codeberg():
    provs = [FakeProvider("codeberg", [mk("codeberg", "s/tie", 5)]),
             FakeProvider("gitlab", [mk("gitlab", "s/tie", 5)]),
             FakeProvider("github", [mk("github", "s/tie", 5)])]
    r = await run(provs)
    assert [x.host for x in r.repos] == ["github"]
    r = await run(provs[:2])
    assert [x.host for x in r.repos] == ["gitlab"]


def test_ordered_three_hosts_equal_stars():
    from repohub.core.search import _ordered
    repos = [mk("codeberg", "a/a", 5), mk("gitlab", "a/a", 5), mk("github", "a/a", 5)]
    assert [x.host for x in _ordered(repos, "stars")] == ["github", "gitlab", "codeberg"]


async def test_dedupe_keeps_higher_ranked_host_on_ties_and_more_stars_otherwise():
    cb = FakeProvider("codeberg", [mk("codeberg", "x/y", 10), mk("codeberg", "t/t", 9)])
    gl = FakeProvider("gitlab", [mk("gitlab", "x/y", 10), mk("gitlab", "t/t", 8)])
    r = await run([cb, gl])
    by = {x.slug: x for x in r.repos}
    assert by["x/y"].host == "gitlab" and by["t/t"].host == "codeberg"


async def test_custom_registry_rank_drives_tie_break():
    from repohub.core.hosts import HostRegistry, HostSpec, set_registry
    set_registry(HostRegistry([HostSpec("mine", "forgejo", "Mine", "git.example.org", "https://git.example.org/api/v1"),
                               HostSpec("github", "github", "GitHub", "github.com", "https://api.github.com")]))
    r = await run([FakeProvider("github", [mk("github", "a/a", 5)]), FakeProvider("mine", [mk("mine", "a/a", 5)])],
                  SearchFilters())
    assert [x.host for x in r.repos] == ["mine"]


async def test_extra_host_never_shadows_a_builtin_entry():
    from repohub.core.hosts import BUILTIN_HOSTS, HostRegistry, HostSpec, set_registry
    set_registry(HostRegistry(BUILTIN_HOSTS + (HostSpec("evil", "forgejo", "e", "evil.example.org",
                                                        "https://evil.example.org/api/v1"),)))
    gh = FakeProvider("github", [mk("github", "torvalds/linux", 200000)])
    ev = FakeProvider("evil", [mk("evil", "Torvalds/Linux", 10_000_000)])
    for order in ([gh, ev], [ev, gh]):
        r = await run(order, SearchFilters(hosts=("github", "evil")))
        assert [(x.host, x.slug) for x in r.repos] == [("github", "torvalds/linux")]
        r = await run(order, SearchFilters(hosts=("evil", "github")))
        assert [x.host for x in r.repos] == ["github"]


async def test_builtin_replaces_an_extra_even_with_fewer_stars():
    from repohub.core.hosts import BUILTIN_HOSTS, HostRegistry, HostSpec, set_registry
    set_registry(HostRegistry(BUILTIN_HOSTS + (HostSpec("evil", "forgejo", "e", "evil.example.org",
                                                        "https://evil.example.org/api/v1"),)))
    ev = FakeProvider("evil", [mk("evil", "a/b", 999)])
    cb = FakeProvider("codeberg", [mk("codeberg", "a/b", 1)])
    r = await run([ev, cb], SearchFilters(hosts=("evil", "codeberg")))
    assert [x.host for x in r.repos] == ["codeberg"]


async def test_two_builtins_still_resolve_by_stars_then_rank():
    gh = FakeProvider("github", [mk("github", "a/b", 5), mk("github", "c/d", 5)])
    cb = FakeProvider("codeberg", [mk("codeberg", "a/b", 9), mk("codeberg", "c/d", 5)])
    r = await run([gh, cb], SearchFilters(hosts=("github", "codeberg")))
    assert {x.slug: x.host for x in r.repos} == {"a/b": "codeberg", "c/d": "github"}
