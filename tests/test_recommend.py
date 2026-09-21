import io
import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

import repohub.cli as cli
from helpers import FakeProvider, mk
from repohub.core import recommend as rec
from repohub.core.accounts import ProviderAccount
from repohub.core.browse import Shelf
from repohub.core.cache import Cache
from repohub.core.history import MAX_AGE, MAX_ENTRIES, History
from repohub.core.hub import Hub
from repohub.core.providers.base import ProviderError, RateLimited
from repohub.core.providers.forgejo import ForgejoProvider
from repohub.core.providers.github import GitHubProvider
from repohub.core.providers.gitlab import GitLabProvider
from repohub.core.store import Favorites
from repohub.web.app import create_app


@pytest.fixture(autouse=True)
def config_dir(monkeypatch, tmp_path):
    cfg = tmp_path / "xdg-config"
    cfg.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg))


def r(slug, topics=(), lang="Rust", stars=100, host="github", **kw):
    return mk(host, slug, stars, topics=tuple(topics), language=lang, **kw)


# ---------------------------------------------------------------- history

def test_history_is_off_by_default_and_records_nothing():
    h = History()
    assert not h.enabled
    h.record(r("o/a"))
    h.set_enabled(True)
    assert h.list() == []


def test_history_records_lists_and_clears():
    h = History()
    h.set_enabled(True)
    h.record(r("o/a"))
    h.record(r("o/b"))
    h.record(r("o/a"))
    assert [x.slug for x in h.list()] == ["o/a", "o/b"]
    h.clear()
    assert h.list() == []


def test_history_turned_off_hides_and_stops_recording():
    h = History()
    h.set_enabled(True)
    h.record(r("o/a"))
    h.set_enabled(False)
    assert h.list() == []
    h.record(r("o/b"))
    h.set_enabled(True)
    assert [x.slug for x in h.list()] == ["o/a"]


def test_history_caps_entries_and_expires_by_age():
    now = [1_000_000.0]
    h = History(now=lambda: now[0])
    h.set_enabled(True)
    for i in range(MAX_ENTRIES + 10):
        now[0] += 1
        h.record(r(f"o/r{i}"))
    assert len(h.list()) == MAX_ENTRIES
    now[0] += MAX_AGE + 5
    assert h.list() == []
    h.record(r("o/new"))
    assert [x.slug for x in h.list()] == ["o/new"]


# ---------------------------------------------------------------- sanitising and profile

@pytest.mark.parametrize("bad", ["x stars:>1", "topic:x", "a b", "<script>", "", "x\x1b[31m", "-lead", "a" * 60, "hacktoberfest", None, 5])
def test_clean_topic_rejects_query_injection_and_noise(bad):
    assert rec.clean_topic(bad) == ""


def test_clean_topic_and_language_accept_normal_values():
    assert rec.clean_topic("TUI") == "tui" and rec.clean_topic("c++") == "c++" and rec.clean_topic("machine-learning")
    assert rec.clean_language("C++") == "C++" and rec.clean_language('Rust" archived:true') == ""


def test_profile_weights_favorites_over_stars_over_history():
    p = rec.build_profile([r("a/a", ["tui"])], [r("b/b", ["cli"])], [r("c/c", ["game"])])
    assert p.topics["tui"] > p.topics["cli"] > p.topics["game"]
    assert p.languages["rust"] == 6.0 and p.top_topics(2) == ["tui", "cli"]


def test_profile_empty_without_usable_data():
    assert rec.build_profile([r("a/a", ["hacktoberfest"], lang="")]).empty
    assert rec.build_profile().empty


def test_plan_queries_at_most_three_with_language_per_topic():
    p = rec.build_profile([r("a/a", ["t1", "t2", "t3", "t4"], lang="Go")])
    plan = rec.plan_queries(p)
    assert len(plan) == 3 and all(q.language == "Go" for q in plan)


# ---------------------------------------------------------------- ranking

def test_rank_excludes_owned_forks_archived_and_is_deterministic():
    p = rec.build_profile([r("a/a", ["tui"])])
    cands = [r("x/1", ["tui"]), r("a/a", ["tui"]), r("x/fork", ["tui"], fork=True), r("x/old", ["tui"], archived=True),
             r("x/2", ["tui"], stars=5), r("x/1", ["tui"])]
    out = rec.rank(cands, p, {"github:a/a"})
    assert [i.repo.slug for i in out] == ["x/1", "x/2"]
    assert out == rec.rank(list(reversed(cands)), p, {"github:a/a"})


def test_rank_diversity_cap_per_leading_topic():
    p = rec.build_profile([r("a/a", ["tui"]), r("b/b", ["cli"])])
    cands = [r(f"x/t{i}", ["tui"], stars=1000 - i) for i in range(6)] + [r("x/c", ["cli"], stars=1)]
    out = rec.rank(cands, p, set())
    assert [i.repo.slug for i in out].count("x/c") == 1
    assert sum(1 for i in out if "tui" in i.repo.topics) == rec.PER_TOPIC_CAP


def test_reason_names_topics_and_sanitises():
    p = rec.build_profile([r("a/a", ["tui", "rust"])])
    why = rec.rank([r("x/1", ["tui", "<b>evil</b>"])], p, set())[0].why
    assert "tui" in why and "<" not in why and "\x1b" not in why


def test_score_orders_by_match_then_popularity():
    p = rec.build_profile([r("a/a", ["tui"])])
    assert rec.score(r("x/1", ["tui"], stars=1), p) > rec.score(r("x/2", ["other"], stars=10_000_000), p)


def test_rank_similar_by_topic_overlap_excluding_self_and_forks():
    base = r("o/base", ["tui", "rust", "cli"])
    cands = [r("x/one", ["tui"]), r("x/two", ["tui", "rust"]), r("o/base", ["tui", "rust"]), r("x/f", ["tui", "rust", "cli"], fork=True),
             r("x/none", ["web"])]
    out = rec.rank_similar(base, cands)
    assert [i.repo.slug for i in out] == ["x/two", "x/one"] and out[0].why == "Shares topics: rust, tui"


def test_similar_plan_uses_clean_topics_only():
    plan = rec.similar_plan(r("o/b", ["tui", "bad topic", "tui", "cli", "x", "y"]))
    assert [q.topic for q in plan] == ["tui", "cli", "x"] and plan[0].language == "Rust"


# ---------------------------------------------------------------- providers: starred_repos

GH_ITEM = {"full_name": "o/r", "html_url": "https://github.com/o/r", "description": "d", "stargazers_count": 5,
           "language": "Rust", "license": None, "topics": ["tui"], "pushed_at": "2026-09-01T00:00:00Z",
           "archived": False, "forks_count": 1, "homepage": None}


@respx.mock
async def test_github_starred_repos_pages_and_caps():
    route = respx.get("https://api.github.com/user/starred").mock(side_effect=[
        httpx.Response(200, json=[GH_ITEM] * 100), httpx.Response(200, json=[GH_ITEM] * 100)])
    got = await GitHubProvider("tok").starred_repos(200)
    assert len(got) == 200 and route.call_count == 2 and route.calls.last.request.url.params["page"] == "2"
    assert route.calls.last.request.headers["authorization"] == "Bearer tok"


@respx.mock
async def test_github_starred_repos_stops_on_short_page_and_needs_token():
    route = respx.get("https://api.github.com/user/starred").mock(return_value=httpx.Response(200, json=[GH_ITEM] * 3))
    assert len(await GitHubProvider("tok").starred_repos()) == 3 and route.call_count == 1
    assert await GitHubProvider().starred_repos() == [] and route.call_count == 1


@respx.mock
async def test_github_starred_repos_malformed():
    respx.get("https://api.github.com/user/starred").mock(return_value=httpx.Response(200, json={"x": 1}))
    with pytest.raises(ProviderError, match="unexpected response"):
        await GitHubProvider("tok").starred_repos()


@respx.mock
async def test_gitlab_starred_repos_uses_user_id():
    respx.get("https://gitlab.com/api/v4/user").mock(return_value=httpx.Response(200, json={"id": 7}))
    route = respx.get("https://gitlab.com/api/v4/users/7/starred_projects").mock(return_value=httpx.Response(200, json=[
        {"path_with_namespace": "g/r", "web_url": "https://gitlab.com/g/r", "star_count": 3, "topics": ["cli"]}]))
    got = await GitLabProvider("tok").starred_repos()
    assert got[0].slug == "g/r" and got[0].topics == ("cli",) and route.calls.last.request.headers["private-token"] == "tok"
    assert await GitLabProvider().starred_repos() == []


@respx.mock
async def test_gitlab_starred_repos_hostile_user_id():
    respx.get("https://gitlab.com/api/v4/user").mock(return_value=httpx.Response(200, json={"id": "7/../../x"}))
    with pytest.raises(ProviderError):
        await GitLabProvider("tok").starred_repos()


@respx.mock
async def test_forgejo_starred_repos_drops_invalid_slugs():
    good = {"full_name": "o/r", "html_url": "https://codeberg.org/o/r", "stars_count": 2, "topics": ["tui"],
            "updated_at": "2026-09-01T00:00:00Z"}
    bad = {**good, "full_name": "../x"}
    respx.get("https://codeberg.org/api/v1/user/starred").mock(return_value=httpx.Response(200, json=[good, bad]))
    got = await ForgejoProvider("codeberg", "https://codeberg.org/api/v1", "tok").starred_repos()
    assert [g.slug for g in got] == ["o/r"]
    assert await ForgejoProvider("codeberg", "https://codeberg.org/api/v1").starred_repos() == []


# ---------------------------------------------------------------- hub

class RecProvider(FakeProvider):
    def __init__(self, host="github", pool=(), starred=(), signed_in=True, starred_error=None):
        super().__init__(host, list(pool))
        self.searches, self.starred, self.signed_in, self.starred_error = [], list(starred), signed_in, starred_error
        self.token_rejected = False
        self.starred_calls = 0

    async def search(self, query, filters, per_page=30):
        self.searches.append(filters)
        return [x for x in self.repos if not filters.topic or filters.topic in x.topics]

    async def account(self):
        return ProviderAccount("me") if self.signed_in else None

    async def starred_repos(self, limit=200):
        self.starred_calls += 1
        if self.starred_error:
            raise self.starred_error
        return list(self.starred)


def hub_of(*providers, clock=None):
    kw = {"clock": clock} if clock else {}
    return Hub({p.host: p for p in providers}, Cache(), Favorites(), **kw)


async def test_no_signal_means_no_searches():
    p = RecProvider(pool=[r("x/1", ["tui"])], signed_in=False)
    res = await hub_of(p).recommend()
    assert res.signal is False and res.items == [] and p.searches == []


async def test_favorites_drive_searches_and_exclude_themselves():
    fav = r("a/fav", ["tui", "cli"])
    p = RecProvider(pool=[fav, r("x/1", ["tui"]), r("x/2", ["cli"])], signed_in=False)
    h = hub_of(p)
    h.favorites.add(fav)
    res = await h.recommend()
    assert {i.repo.slug for i in res.items} == {"x/1", "x/2"} and 1 <= len(p.searches) <= 3
    assert all(i.why for i in res.items) and all(f.hide_forks for f in p.searches)


async def test_starred_repos_count_and_are_excluded():
    s = r("a/starred", ["uniquetopicxyz"])
    p = RecProvider(pool=[s, r("x/1", ["uniquetopicxyz"])], starred=[s])
    res = await hub_of(p).recommend()
    assert res.signal and [i.repo.slug for i in res.items] == ["x/1"]


async def test_starred_list_is_never_written_to_disk(tmp_path):
    s = r("a/starred", ["uniquetopicxyz"])
    p = RecProvider(pool=[r("x/1", ["uniquetopicxyz"])], starred=[s])  # the host's search never returns it
    db = str(tmp_path / "h.db")
    h = Hub({"github": p}, Cache(db), Favorites(db))
    assert (await h.recommend()).signal
    assert b"a/starred" not in (tmp_path / "h.db").read_bytes()


async def test_at_most_three_searches():
    fav = r("a/fav", ["t1", "t2", "t3", "t4", "t5"])
    p = RecProvider(pool=[], signed_in=False)
    h = hub_of(p)
    h.favorites.add(fav)
    await h.recommend()
    assert len(p.searches) == 3


async def test_history_only_counts_when_on():
    seen = r("a/seen", ["tui"])
    p = RecProvider(pool=[seen, r("x/1", ["tui"])], signed_in=False)
    h = hub_of(p)
    h.record_view(seen)
    assert (await h.recommend()).signal is False
    h.history.set_enabled(True)
    h.record_view(seen)
    res = await h.recommend()
    assert res.signal and [i.repo.slug for i in res.items] == ["x/1"]


async def test_result_cached_for_an_hour():
    now = [1000.0]
    p = RecProvider(pool=[r("x/1", ["tui"])], signed_in=False)
    h = hub_of(p, clock=lambda: now[0])
    h.favorites.add(r("a/fav", ["tui"]))
    first = await h.recommend()
    key = next(iter(h._rec_memo))
    assert h._memo(key) is not None and (await h.recommend()).items == first.items
    now[0] += 3601
    assert h._memo(key) is None


async def test_rate_limited_host_is_skipped_for_starred_and_searches():
    p = RecProvider(pool=[r("x/1", ["tui"])], starred=[r("a/s", ["tui"])], starred_error=RateLimited("github", "slow", None))
    h = hub_of(p)
    h.favorites.add(r("a/fav", ["tui"]))
    res = await h.recommend()
    assert "github" in res.errors and p.starred_calls == 1
    p.searches.clear()
    res2 = await h.recommend()
    assert p.starred_calls == 1 and p.searches == []  # paused: no requests at all
    assert res2.items == []


async def test_provider_errors_are_reported_not_raised():
    p = RecProvider(starred_error=ProviderError("github", "network error"))
    h = hub_of(p)
    h.favorites.add(r("a/fav", ["tui"]))
    res = await h.recommend()
    assert res.errors["github"] == "network error" and res.signal


async def test_similar_searches_topics_and_excludes_self():
    base = r("o/base", ["tui", "cli"])
    p = RecProvider(pool=[base, r("x/1", ["tui", "cli"]), r("x/2", ["tui"]), r("x/fk", ["tui"], fork=True)])
    res = await hub_of(p).similar("github", "o/base")
    assert [i.repo.slug for i in res.items] == ["x/1", "x/2"] and len(p.searches) == 2


# ---------------------------------------------------------------- web

TOKEN = "sess"


def web(p, tmp_path):
    h = hub_of(p)
    app = create_app(h, tmp_path, session_token=TOKEN, shelves=[Shelf("s", topic="x")])
    return TestClient(app, base_url="http://localhost"), h


def test_recommended_fragment_empty_without_signal(tmp_path):
    c, _ = web(RecProvider(signed_in=False), tmp_path)
    assert c.get("/recommended").text == ""
    assert 'hx-get="/recommended"' in c.get("/").text


def test_recommended_fragment_shows_why_and_escapes(tmp_path):
    p = RecProvider(pool=[r("x/1", ["tui"], description="<script>alert(1)</script>")], signed_in=False)
    c, h = web(p, tmp_path)
    h.favorites.add(r("a/fav", ["tui"]))
    t = c.get("/recommended").text
    assert "x/1" in t and "Matches your interest in tui" in t and "<script>alert(1)</script>" not in t


def test_similar_fragment_and_repo_page_hook(tmp_path):
    p = RecProvider(pool=[r("o/base", ["tui"]), r("x/1", ["tui"])], signed_in=False)
    c, _ = web(p, tmp_path)
    assert "x/1" in c.get("/similar/github/o/base").text
    assert 'hx-get="/similar/github/o/base"' in c.get("/repo/github/o/base").text
    assert c.get("/similar/github/../x").status_code == 404


def test_history_controls_need_token_and_toggle(tmp_path):
    c, h = web(RecProvider(signed_in=False), tmp_path)
    assert c.post("/history", data={"action": "on"}).status_code == 403
    assert c.post("/history", data={"action": "on", "token": "bad"}).status_code == 403
    assert not h.history.enabled
    resp = c.post("/history", data={"action": "on", "token": TOKEN}, follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/accounts" and h.history.enabled
    assert "Local history is <strong>on</strong>" in c.get("/accounts").text
    assert c.post("/history", data={"action": "explode", "token": TOKEN}).status_code == 404
    c.post("/history", data={"action": "off", "token": TOKEN})
    assert not h.history.enabled


def test_opening_a_repo_page_records_history_only_when_on(tmp_path):
    p = RecProvider(pool=[r("o/base", ["tui"])], signed_in=False)
    c, h = web(p, tmp_path)
    c.get("/repo/github/o/base")
    h.history.set_enabled(True)
    assert h.history.list() == []
    c.get("/repo/github/o/base")
    assert [x.slug for x in h.history.list()] == ["o/base"]
    c.post("/history", data={"action": "clear", "token": TOKEN})
    assert h.history.list() == []


# ---------------------------------------------------------------- CLI

def run(argv, hub):
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(argv, hub_factory=lambda: hub, stdout=out, stderr=err, gh_cli=lambda: None)
    return code, out.getvalue(), err.getvalue()


def test_cli_recommend_json_and_table():
    p = RecProvider(pool=[r("x/1", ["tui"])], signed_in=False)
    h = hub_of(p)
    h.favorites.add(r("a/fav", ["tui"]))
    code, out, _ = run(["recommend", "--json"], h)
    doc = json.loads(out)
    assert code == 0 and doc["recommendations"][0]["slug"] == "x/1" and "tui" in doc["recommendations"][0]["why"]
    code, out, _ = run(["recommend"], h)
    assert "x/1" in out and "why" in out


def test_cli_recommend_without_signal_says_so():
    code, out, err = run(["recommend"], hub_of(RecProvider(signed_in=False)))
    assert code == 0 and "favorite or star" in err and out == ""


def test_cli_similar_and_bad_argument():
    p = RecProvider(pool=[r("o/base", ["tui"]), r("x/1", ["tui"])], signed_in=False)
    code, out, _ = run(["similar", "github:o/base", "--json"], hub_of(p))
    assert code == 0 and json.loads(out)["recommendations"][0]["slug"] == "x/1"
    code, _, err = run(["similar", "nonsense"], None)  # rejected before any hub is built
    assert code == 2 and "HOST:OWNER/NAME" in err


def test_cli_never_changes_history_or_settings():
    h = hub_of(RecProvider(pool=[r("o/base", ["tui"])], signed_in=False))
    run(["similar", "github:o/base"], h)
    assert not h.history.enabled and h.history.list() == []


# ---------------------------------------------------------------- terminal app

async def test_tui_recommended_row_and_history_keys(tmp_path):
    from textual.widgets import DataTable

    from repohub.tui.app import RepoHubApp

    p = RecProvider(pool=[r("x/1", ["tui"])], signed_in=False)
    h = hub_of(p)
    h.favorites.add(r("a/fav", ["tui"]))
    app = RepoHubApp(h, tmp_path, shelves=[Shelf("s", topic="x")])
    async with app.run_test() as pilot:
        t = app.query_one(DataTable)
        assert [k.value for k in t.rows][-1] == "recommended"
        t.move_cursor(row=t.row_count - 1)
        t.focus()
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.view == "recommended" and app.query_one(DataTable).row_count == 1
        await pilot.press("f3")
        assert h.history.enabled
        await pilot.press("f3")
        assert not h.history.enabled


# ---------------------------------------------------------------- review follow-ups

async def test_one_failing_search_does_not_lose_the_others():
    class Flaky(RecProvider):
        async def search(self, query, filters, per_page=30):
            if filters.topic == "boom":
                raise RuntimeError("x")
            return await super().search(query, filters, per_page)

    p = Flaky(pool=[r("x/1", ["tui"])], signed_in=False)
    h = hub_of(p)
    h.favorites.add(r("a/fav", ["boom", "tui"]))
    res = await h.recommend()
    assert [i.repo.slug for i in res.items] == ["x/1"]


async def test_caller_cancellation_still_propagates():
    import asyncio

    class Slow(RecProvider):
        async def search(self, query, filters, per_page=30):
            await asyncio.sleep(30)

    h = hub_of(Slow(signed_in=False))
    h.favorites.add(r("a/fav", ["tui"]))
    task = asyncio.ensure_future(h.recommend())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_corrupt_history_row_is_skipped(tmp_path):
    db = str(tmp_path / "h.db")
    h = History(db)
    h.set_enabled(True)
    h.record(r("o/good"))
    h._db.execute("INSERT INTO history (key, data, at) VALUES ('bad', 'not json', ?)", (h._now(),))
    h._db.commit()
    assert [x.slug for x in h.list()] == ["o/good"]


@respx.mock
async def test_tokenless_starred_repos_make_no_request():
    gl = respx.get("https://gitlab.com/api/v4/user").mock(return_value=httpx.Response(200, json={"id": 1}))
    cb = respx.get("https://codeberg.org/api/v1/user/starred").mock(return_value=httpx.Response(200, json=[]))
    assert await GitLabProvider().starred_repos() == []
    assert await ForgejoProvider("codeberg", "https://codeberg.org/api/v1").starred_repos() == []
    assert gl.call_count == 0 and cb.call_count == 0


async def test_paused_host_is_not_asked_for_starred_even_with_a_cache_miss():
    p = RecProvider(pool=[r("x/1", ["tui"])], starred=[r("a/s", ["tui"])])
    h = hub_of(p)
    h._note_rate_limit("github", RateLimited("github", "slow", None))
    h.favorites.add(r("a/fav", ["tui"]))
    res = await h.recommend()
    assert p.starred_calls == 0 and p.searches == [] and "github" in res.errors


def test_accounts_page_history_clear_is_confirmed_and_explained(tmp_path):
    c, _ = web(RecProvider(signed_in=False), tmp_path)
    t = c.get("/accounts").text
    assert "hx-confirm" in t and "only hides what is kept" in t


async def test_tui_clear_history_needs_confirmation(tmp_path):
    from repohub.tui.app import ConfirmWrite, RepoHubApp

    h = hub_of(RecProvider(signed_in=False))
    h.history.set_enabled(True)
    h.history.record(r("o/a"))
    app = RepoHubApp(h, tmp_path, shelves=[Shelf("s", topic="x")])
    async with app.run_test() as pilot:
        await pilot.press("f4")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmWrite) and len(h.history.list()) == 1
        await pilot.press("n")
        await pilot.pause()
        assert len(h.history.list()) == 1
        await pilot.press("f4")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        assert h.history.list() == []
