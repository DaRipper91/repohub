import io
import json

import pytest
from fastapi.testclient import TestClient

import repohub.cli as cli
from helpers import FakeProvider, make_hub, mk
from repohub.core.browse import Shelf
from repohub.core.cache import Cache
from repohub.core.hub import Hub
from repohub.core.models import Release
from repohub.core.providers.base import ProviderError, RateLimited
from repohub.core.store import MAX_COLLECTIONS, MAX_NOTE, MAX_PER_COLLECTION, MAX_TAGS, Favorites
from repohub.web.app import create_app

TOKEN = "t"


@pytest.fixture(autouse=True)
def config_dir(monkeypatch, tmp_path):
    cfg = tmp_path / "xdg-config"
    cfg.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg))


def favs_with(*slugs, now=None):
    f = Favorites(now=now) if now else Favorites()
    for slug in slugs:
        f.add(mk("github", slug, 5))
    return f


# ---------------------------------------------------------------- notes

def test_note_roundtrip_clean_cap_and_clear():
    f = favs_with("o/a")
    assert f.set_note("github:o/a", "  my  note\x1b[31m\n line2 ") is True
    assert f.note("github:o/a") == "my note[31m line2" and "\x1b" not in f.note("github:o/a")  # the ESC is gone
    f.set_note("github:o/a", "x" * 900)
    assert len(f.note("github:o/a")) == MAX_NOTE
    f.set_note("github:o/a", "   ")
    assert f.note("github:o/a") == ""


def test_note_only_for_a_favorite():
    assert favs_with("o/a").set_note("github:o/none", "x") is False


# ---------------------------------------------------------------- tags

@pytest.mark.parametrize("bad", ["", "  ", "-x", "a" * 31, "a;b", "<b>", "a\x1b[1m", "tag:with:colon", "ü" * 3])
def test_bad_tags_rejected(bad):
    f = favs_with("o/a")
    assert f.add_tag("github:o/a", bad) is False and f.tags("github:o/a") == []


def test_tags_are_lowercased_unique_and_capped():
    f = favs_with("o/a")
    assert f.add_tag("github:o/a", " TUI ") and not f.add_tag("github:o/a", "tui")
    for i in range(MAX_TAGS + 3):
        f.add_tag("github:o/a", f"t{i}")
    assert len(f.tags("github:o/a")) == MAX_TAGS and f.tags("github:o/a")[0] == "t0"
    f.remove_tag("github:o/a", "T0")
    assert "t0" not in f.tags("github:o/a")


def test_tag_only_for_a_favorite_and_counts():
    f = favs_with("o/a", "o/b")
    assert f.add_tag("github:o/none", "x") is False
    f.add_tag("github:o/a", "tui")
    f.add_tag("github:o/b", "tui")
    f.add_tag("github:o/b", "cli")
    assert f.all_tags() == [("cli", 1), ("tui", 2)]


# ---------------------------------------------------------------- collections

def test_collections_create_add_list_delete():
    f = favs_with("o/a", "o/b")
    assert f.create_collection("Work stuff") and not f.create_collection("Work stuff")
    assert f.add_to_collection("Work stuff", "github:o/a") and not f.add_to_collection("Work stuff", "github:o/a")
    assert f.collections() == [("Work stuff", 1)] and f.collections_by_key() == {"github:o/a": ["Work stuff"]}
    f.remove_from_collection("Work stuff", "github:o/a")
    assert f.collections() == [("Work stuff", 0)]
    f.add_to_collection("Work stuff", "github:o/b")
    f.delete_collection("Work stuff")
    assert f.collections() == [] and f.collections_by_key() == {} and len(f.list()) == 2  # favorites stay


@pytest.mark.parametrize("bad", ["", " ", "-x", "a" * 41, "a<b>", "x\x1b[1m", "a/b"])
def test_bad_collection_names_rejected(bad):
    assert favs_with().create_collection(bad) is False


def test_collection_caps_and_missing_targets():
    f = favs_with("o/a")
    for i in range(MAX_COLLECTIONS + 3):
        f.create_collection(f"c{i}")
    assert len(f.collections()) == MAX_COLLECTIONS
    assert f.add_to_collection("c0", "github:o/none") is False and f.add_to_collection("nope", "github:o/a") is False
    assert MAX_PER_COLLECTION >= 100


def test_removing_a_favorite_removes_its_notes_tags_memberships_and_release_rows():
    f = favs_with("o/a", "o/b")
    f.set_note("github:o/a", "n")
    f.add_tag("github:o/a", "t")
    f.create_collection("c")
    f.add_to_collection("c", "github:o/a")
    f.record_release("github:o/a", "v1", "2026-01-01")
    f.remove("github:o/a")
    assert f.notes() == {} and f.tags_by_key() == {} and f.collections() == [("c", 0)] and f.new_releases() == []


# ---------------------------------------------------------------- filtering

def test_filter_by_tag_collection_and_text_including_notes():
    f = favs_with("o/alpha", "o/beta", "o/gamma")
    f.add_tag("github:o/alpha", "tui")
    f.add_tag("github:o/beta", "tui")
    f.create_collection("Keep")
    f.add_to_collection("Keep", "github:o/beta")
    f.set_note("github:o/gamma", "remember the zebra thing")
    assert {r.slug for r in f.list(tag="tui")} == {"o/alpha", "o/beta"}
    assert [r.slug for r in f.list(collection="Keep")] == ["o/beta"]
    assert [r.slug for r in f.list(tag="tui", collection="Keep")] == ["o/beta"]
    assert [r.slug for r in f.list(q="ZEBRA")] == ["o/gamma"]
    assert [r.slug for r in f.list(q="alp")] == ["o/alpha"] and f.list(tag="nope") == []
    assert len(f.list()) == 3


def test_filter_values_are_bound_not_interpolated():
    f = favs_with("o/a")
    assert f.list(tag="x' OR '1'='1") == [] and f.list(collection="'; DROP TABLE favorites;--") == []
    assert len(f.list()) == 1


# ---------------------------------------------------------------- release tracking

def test_first_release_is_a_baseline_then_a_newer_one_is_new_until_seen():
    f = favs_with("o/a")
    f.record_release("github:o/a", "v1", "2026-01-01T00:00:00Z")
    assert f.new_releases() == []
    f.record_release("github:o/a", "v2", "2026-02-01T00:00:00Z")
    (r, tag, pub), = f.new_releases()
    assert (r.slug, tag, pub[:10]) == ("o/a", "v2", "2026-02-01")
    f.mark_release_seen("github:o/a")
    assert f.new_releases() == []
    f.record_release("github:o/a", "v3", None)
    assert [t for _, t, _ in f.new_releases()] == ["v3"]
    f.mark_release_seen()
    assert f.new_releases() == []


def test_release_tags_are_cleaned_and_only_for_favorites():
    f = favs_with("o/a")
    f.record_release("github:o/none", "v1", None)
    f.record_release("github:o/a", "v1", None)
    f.record_release("github:o/a", "v2\x1b[31m", None)
    assert f.new_releases()[0][1] == "v2[31m" and "\x1b" not in f.new_releases()[0][1]


# ---------------------------------------------------------------- hub: check_releases

class RelProvider(FakeProvider):
    def __init__(self, releases, error=None):
        super().__init__("github", [])
        self.rel, self.err, self.n = releases, error, 0

    async def latest_release(self, slug):
        self.n += 1
        if self.err:
            raise self.err
        return self.rel.get(slug)


def hub_with(p, *slugs, clock=None):
    h = Hub({p.host: p}, Cache(), Favorites(), **({"clock": clock} if clock else {}))
    for s in slugs:
        h.favorites.add(mk("github", s, 5))
    return h


async def test_check_releases_records_and_detects_new():
    p = RelProvider({"o/a": Release("v1", "2026-01-01T00:00:00Z", ())})
    h = hub_with(p, "o/a", "o/b")
    assert await h.check_releases() == {} and h.favorites.new_releases() == []
    p.rel["o/a"] = Release("v2", "2026-03-01T00:00:00Z", ())
    await h.check_releases(force=True)
    assert [t for _, t, _ in h.favorites.new_releases()] == ["v2"]


async def test_check_releases_is_throttled_for_half_an_hour():
    now = [1000.0]
    p = RelProvider({"o/a": Release("v1", None, ())})
    h = hub_with(p, "o/a", clock=lambda: now[0])
    await h.check_releases()
    await h.check_releases()
    assert p.n == 1
    now[0] += 1801
    await h.check_releases()
    assert p.n == 2


async def test_check_releases_reports_errors_and_respects_rate_limit():
    p = RelProvider({}, error=RateLimited("github", "slow", None))
    h = hub_with(p, "o/a", "o/b")
    assert "github" in await h.check_releases()
    n = p.n
    assert "github" in await h.check_releases(force=True) and p.n == n  # paused: no more requests
    q = RelProvider({}, error=ProviderError("github", "network error"))
    assert (await hub_with(q, "o/a").check_releases())["github"] == "network error"
    boom = RelProvider({}, error=RuntimeError("x"))
    assert (await hub_with(boom, "o/a").check_releases())["github"] == "unexpected error"


async def test_check_releases_makes_no_call_for_unconfigured_hosts():
    p = RelProvider({})
    h = Hub({"github": p}, Cache(), Favorites())
    h.favorites.add(mk("gitlab", "g/x", 1))
    assert await h.check_releases() == {} and p.n == 0


# ---------------------------------------------------------------- web

def web(tmp_path, *slugs, releases=None):
    p = RelProvider(releases or {})
    p.repos = [mk("github", s, 5) for s in slugs]
    h = Hub({"github": p}, Cache(), Favorites())
    for s in slugs:
        h.favorites.add(mk("github", s, 5))
    app = create_app(h, tmp_path, session_token=TOKEN, shelves=[Shelf("s", topic="x")])
    return TestClient(app, base_url="http://localhost"), h


def post(c, url, **data):
    return c.post(url, data={"token": TOKEN, **data}, follow_redirects=False)


def test_all_write_routes_need_the_session_token(tmp_path):
    c, h = web(tmp_path, "o/a")
    for url, data in (("/favorites/note", {"host": "github", "slug": "o/a", "note": "x"}),
                      ("/favorites/tag", {"host": "github", "slug": "o/a", "tag": "x"}),
                      ("/favorites/collection", {"action": "create", "name": "c"}),
                      ("/favorites/seen", {})):
        assert c.post(url, data=data).status_code == 403
        assert c.post(url, data={**data, "token": "bad"}).status_code == 403
    assert h.favorites.note("github:o/a") == "" and h.favorites.collections() == [] and h.favorites.tags("github:o/a") == []


def test_web_note_tag_collection_flow_and_rendering(tmp_path):
    c, h = web(tmp_path, "o/a", "o/b")
    assert post(c, "/favorites/note", host="github", slug="o/a", note="hello <b>bold</b>").headers["location"] == "/favorites"
    post(c, "/favorites/tag", host="github", slug="o/a", tag="TUI", action="add")
    post(c, "/favorites/collection", action="create", name="Shortlist")
    post(c, "/favorites/collection", action="add", name="Shortlist", host="github", slug="o/a")
    t = c.get("/favorites").text
    assert "hello &lt;b&gt;bold&lt;/b&gt;" in t and "<b>bold</b>" not in t
    assert 'class="tag">tui' in t and "Shortlist" in t and "onclick" not in t
    assert [r.slug for r in h.favorites.list(tag="tui")] == ["o/a"]
    only = c.get("/favorites", params={"tag": "tui"}).text
    assert "o/a" in only and "o/b" not in only
    assert "o/b" not in c.get("/favorites", params={"collection": "Shortlist"}).text
    post(c, "/favorites/tag", host="github", slug="o/a", tag="tui", action="remove")
    post(c, "/favorites/collection", action="remove", name="Shortlist", host="github", slug="o/a")
    post(c, "/favorites/collection", action="delete", name="Shortlist")
    assert h.favorites.tags("github:o/a") == [] and h.favorites.collections() == []


def test_web_rejects_non_favorites_bad_actions_and_long_slugs(tmp_path):
    c, h = web(tmp_path, "o/a")
    assert post(c, "/favorites/note", host="github", slug="o/other", note="x").status_code == 404
    assert post(c, "/favorites/tag", host="github", slug="o/a", tag="x", action="explode").status_code == 404
    assert post(c, "/favorites/collection", action="explode").status_code == 404
    assert post(c, "/favorites/tag", host="github", slug="o/" + "a" * 500, tag="x").status_code == 404
    post(c, "/favorites/tag", host="github", slug="o/a", tag="<script>", action="add")
    assert h.favorites.tags("github:o/a") == []


def test_favorites_filters_with_hostile_query_values(tmp_path):
    c, _ = web(tmp_path, "o/a")
    r = c.get("/favorites", params={"tag": "<script>alert(1)</script>", "collection": "x" * 500, "q": "<img src=x onerror=1>"})
    assert r.status_code == 200 and "<script>alert(1)</script>" not in r.text and "<img src=x" not in r.text


def test_releases_page_and_seen_flow(tmp_path):
    p_rel = {"o/a": Release("v1", "2026-01-01T00:00:00Z", ())}
    c, h = web(tmp_path, "o/a", releases=p_rel)
    assert "Nothing new" in c.get("/favorites/releases").text  # baseline only
    h.providers["github"].rel["o/a"] = Release("v2", "2026-05-01T00:00:00Z", ())
    h._releases_checked = None
    t = c.get("/favorites/releases").text
    assert "v2" in t and "2026-05-01" in t and "Mark as seen" in t
    assert "(1)" in c.get("/favorites").text
    assert c.post("/favorites/seen", data={"host": "github", "slug": "o/a"}).status_code == 403
    post(c, "/favorites/seen", host="github", slug="o/a")
    assert "Nothing new" in c.get("/favorites/releases").text


def test_seen_all_and_unknown_favorite(tmp_path):
    c, h = web(tmp_path, "o/a")
    assert post(c, "/favorites/seen", host="github", slug="o/none").status_code == 404
    h.favorites.record_release("github:o/a", "v1", None)
    h.favorites.record_release("github:o/a", "v2", None)
    post(c, "/favorites/seen")
    assert h.favorites.new_releases() == []


# ---------------------------------------------------------------- CLI

def run(argv, hub):
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(argv, hub_factory=lambda: hub, stdout=out, stderr=err, gh_cli=lambda: None)
    return code, out.getvalue(), err.getvalue()


def test_cli_favorites_filters_and_json_metadata():
    h = hub_with(RelProvider({}), "o/alpha", "o/beta")
    h.favorites.add_tag("github:o/alpha", "tui")
    h.favorites.set_note("github:o/beta", "zebra")
    h.favorites.create_collection("Keep")
    h.favorites.add_to_collection("Keep", "github:o/alpha")
    code, out, _ = run(["favorites", "--tag", "TUI", "--json"], h)
    (item,) = json.loads(out)["repos"]
    assert code == 0 and item["slug"] == "o/alpha" and item["tags"] == ["tui"] and item["collections"] == ["Keep"]
    assert [r["slug"] for r in json.loads(run(["favorites", "--collection", "Keep", "--json"], h)[1])["repos"]] == ["o/alpha"]
    assert [r["slug"] for r in json.loads(run(["favorites", "--query", "zebra", "--json"], h)[1])["repos"]] == ["o/beta"]
    assert run(["favorites", "--tag", "<bad>"], h)[0] == 2


def test_cli_releases_lists_new_and_never_marks_seen():
    p = RelProvider({"o/a": Release("v1", "2026-01-01T00:00:00Z", ())})
    h = hub_with(p, "o/a")
    assert run(["releases"], h)[0] == 0  # baseline
    p.rel["o/a"] = Release("v2", "2026-06-01T00:00:00Z", ())
    code, out, _ = run(["releases", "--json"], h)
    assert code == 0 and json.loads(out)["new_releases"][0]["release"] == "v2"
    assert len(h.favorites.new_releases()) == 1  # still unseen: the CLI has no way to mark
    code, out, _ = run(["releases"], h)
    assert "v2" in out


def test_cli_releases_exit_codes():
    err_hub = hub_with(RelProvider({}, error=ProviderError("github", "network error")), "o/a")
    code, _, err = run(["releases"], err_hub)
    assert code == 1 and "network error" in err


# ---------------------------------------------------------------- terminal app

async def test_tui_releases_view_and_mark_seen(tmp_path):
    from textual.widgets import DataTable

    from repohub.tui.app import RepoHubApp

    p = RelProvider({"o/a": Release("v1", "2026-01-01T00:00:00Z", ())})
    p.repos = [mk("github", "o/a", 5)]
    h = hub_with(p, "o/a")
    app = RepoHubApp(h, tmp_path, shelves=[Shelf("s", topic="x")])
    async with app.run_test() as pilot:
        await pilot.press("ctrl+f")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.view == "favorites"
        app.query_one(DataTable).focus()
        await pilot.press("r")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.view == "releases" and app.query_one(DataTable).row_count == 0  # baseline
        p.rel["o/a"] = Release("v2", "2026-06-01T00:00:00Z", ())
        h._releases_checked = None
        await pilot.press("r")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.query_one(DataTable).row_count == 1
        app.query_one(DataTable).focus()
        await pilot.press("m")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert h.favorites.new_releases() == [] and app.query_one(DataTable).row_count == 0


async def test_tui_r_and_m_do_nothing_outside_favorites(tmp_path):
    from repohub.tui.app import RepoHubApp

    h = hub_with(RelProvider({}), "o/a")
    app = RepoHubApp(h, tmp_path, shelves=[Shelf("s", topic="x")])
    async with app.run_test() as pilot:
        app.query_one("DataTable").focus()
        await pilot.press("r", "m")
        await pilot.pause()
        assert app.view == "shelves"


async def test_tui_detail_shows_tags_collections_and_note(tmp_path):
    from textual.widgets import Static

    from repohub.tui.app import DetailScreen, RepoHubApp

    p = RelProvider({})
    p.repos = [mk("github", "o/a", 5)]
    h = hub_with(p, "o/a")
    h.favorites.add_tag("github:o/a", "tui")
    h.favorites.set_note("github:o/a", "my note")
    h.favorites.create_collection("Keep")
    h.favorites.add_to_collection("Keep", "github:o/a")
    app = RepoHubApp(h, tmp_path, shelves=[Shelf("s", topic="x")])
    async with app.run_test() as pilot:
        app.push_screen(DetailScreen(h, "github", "o/a", tmp_path, app.cloner))
        await app.workers.wait_for_complete()
        await pilot.pause()
        text = str(app.screen.query_one("#meta", Static).render())
        assert "Tags: tui" in text and "Collections: Keep" in text and "Note: my note" in text


# ---------------------------------------------------------------- review follow-ups

async def test_a_cancelled_check_does_not_block_the_next_one():
    import asyncio

    class Slow(RelProvider):
        async def latest_release(self, slug):
            await asyncio.sleep(30)

    p = Slow({})
    h = hub_with(p, "o/a")
    task = asyncio.ensure_future(h.check_releases())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    p2 = RelProvider({"o/a": Release("v1", "2026-01-01T00:00:00Z", ())})
    h.providers["github"] = p2
    await h.check_releases()  # not throttled: the cancelled run never counted
    assert p2.n == 1


async def test_errors_are_retried_after_a_minute_and_repeated_when_throttled():
    now = [1000.0]
    p = RelProvider({}, error=ProviderError("github", "network error"))
    h = hub_with(p, "o/a", clock=lambda: now[0])
    assert (await h.check_releases())["github"] == "network error"
    assert (await h.check_releases())["github"] == "network error" and p.n == 1  # throttled, but the error is still shown
    now[0] += 61
    await h.check_releases()
    assert p.n == 2


async def test_one_failing_repository_does_not_stop_the_others_on_its_host():
    class Picky(RelProvider):
        async def latest_release(self, slug):
            if slug == "o/a":
                raise ProviderError("github", "HTTP 500")
            return Release("v1", "2026-01-01T00:00:00Z", ())

    h = hub_with(Picky({}), "o/a", "o/b", "o/c")
    errors = await h.check_releases()
    assert errors == {"github": "HTTP 500"}
    assert set(h.favorites._db.execute("SELECT key FROM fav_release").fetchall()) == {("github:o/b",), ("github:o/c",)}


def test_the_first_release_of_a_favorite_that_had_none_counts_as_new():
    f = favs_with("o/a")
    f.record_no_release("github:o/a")
    assert f.new_releases() == []
    f.record_release("github:o/a", "v1.0.0", "2026-03-01T00:00:00Z")
    assert [t for _, t, _ in f.new_releases()] == ["v1.0.0"]


def test_a_deleted_or_retagged_latest_release_does_not_show_as_new():
    f = favs_with("o/a")
    f.record_release("github:o/a", "v2", "2026-02-01T00:00:00Z")  # baseline: seen
    f.record_release("github:o/a", "v1", "2026-01-01T00:00:00Z")  # v2 was deleted: the latest is now OLDER
    assert f.new_releases() == []
    f.record_release("github:o/a", "v3", "2026-03-01T00:00:00Z")
    assert [t for _, t, _ in f.new_releases()] == ["v3"]


def test_mark_seen_with_a_stale_tag_leaves_a_newer_release_unseen():
    f = favs_with("o/a")
    f.record_release("github:o/a", "v1", "2026-01-01T00:00:00Z")
    f.record_release("github:o/a", "v2", "2026-02-01T00:00:00Z")
    f.record_release("github:o/a", "v3", "2026-03-01T00:00:00Z")  # landed after the page showing v2 was rendered
    f.mark_release_seen("github:o/a", "v2")
    assert [t for _, t, _ in f.new_releases()] == ["v3"]
    f.mark_release_seen("github:o/a", "v3")
    assert f.new_releases() == []


def test_an_older_database_gets_the_new_column(tmp_path):
    import sqlite3

    db = str(tmp_path / "old.db")
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE fav_release (key TEXT PRIMARY KEY, latest_tag TEXT NOT NULL, published_at TEXT, "
              "checked_at REAL NOT NULL, seen_tag TEXT)")
    c.commit()
    c.close()
    f = Favorites(db)
    f.add(mk("github", "o/a", 1))
    f.record_release("github:o/a", "v1", "2026-01-01T00:00:00Z")
    f.record_release("github:o/a", "v2", "2026-02-01T00:00:00Z")
    assert [t for _, t, _ in f.new_releases()] == ["v2"]
    Favorites(db)  # opening twice is fine


def test_the_seen_form_carries_the_displayed_tag(tmp_path):
    c, h = web(tmp_path, "o/a")
    h.favorites.record_release("github:o/a", "v1", "2026-01-01T00:00:00Z")
    h.favorites.record_release("github:o/a", "v2", "2026-02-01T00:00:00Z")
    assert 'name="tag" value="v2"' in c.get("/favorites/releases").text
    h.favorites.record_release("github:o/a", "v3", "2026-03-01T00:00:00Z")
    post(c, "/favorites/seen", host="github", slug="o/a", tag="v2")  # a stale click
    assert [t for _, t, _ in h.favorites.new_releases()] == ["v3"]
