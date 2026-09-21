from helpers import FakeProvider, make_hub, mk
from textual.widgets import DataTable, Input

from repohub.core.browse import Shelf
from repohub.core.models import Asset, Release
from repohub.tui.app import DetailScreen, RepoHubApp


def make_app(tmp_path, cloner=None):
    rel = Release("v1", None, (Asset("t-arm64.tgz", 1, "u", "arm64"),))
    gh = FakeProvider("github", [mk("github", "o/r", 50), mk("github", "p/q", 5)], release=rel)
    hub = make_hub(gh)
    return RepoHubApp(hub, tmp_path, shelves=[Shelf("Terminal tools", topic="tui")],
                      cloner=cloner or (lambda url, root: tmp_path / "r")), hub


async def settle(app, pilot):
    await app.workers.wait_for_complete()
    await pilot.pause()


async def test_starts_with_shelf_list(tmp_path):
    app, _ = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.query_one(DataTable).row_count == 2


async def test_search_fills_results(tmp_path):
    app, _ = make_app(tmp_path)
    async with app.run_test() as pilot:
        app.query_one(Input).focus()
        await pilot.press(*"tui", "enter")
        await settle(app, pilot)
        assert app.query_one(DataTable).row_count == 2


async def test_selecting_a_repo_opens_detail_and_toggles_favorite(tmp_path):
    app, hub = make_app(tmp_path)
    async with app.run_test() as pilot:
        app.query_one(Input).focus()
        await pilot.press(*"tui", "enter")
        await settle(app, pilot)
        app.query_one(DataTable).focus()
        await pilot.press("enter")
        await settle(app, pilot)
        assert isinstance(app.screen, DetailScreen)
        assert app.screen.detail.repo.slug == "o/r" and app.screen.detail.release.has_arm64
        await pilot.press("f")
        await pilot.pause()
        assert hub.favorites.is_favorite("github:o/r")
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, DetailScreen)


async def test_clone_requires_confirmation(tmp_path):
    calls = []
    app, _ = make_app(tmp_path, cloner=lambda url, root: calls.append((url, root)) or tmp_path / "r")
    async with app.run_test() as pilot:
        app.query_one(Input).focus()
        await pilot.press(*"tui", "enter")
        await settle(app, pilot)
        app.query_one(DataTable).focus()
        await pilot.press("enter")
        await settle(app, pilot)
        await pilot.press("c")
        await pilot.pause()
        assert calls == []                      # nothing runs until confirmed
        await pilot.press("n")
        await settle(app, pilot)
        assert calls == []
        await pilot.press("c")
        await pilot.pause()
        await pilot.press("y")
        await settle(app, pilot)
        assert calls == [("https://github.com/o/r.git", tmp_path)]


# ---- robustness ----------------------------------------------------------------------------

import asyncio  # noqa: E402

from repohub.core.clone import CloneError  # noqa: E402
from repohub.core.providers.base import ProviderError  # noqa: E402
from textual.widgets import Markdown, Static  # noqa: E402

EVIL = [
    mk("github", "o/[/]", 9, description="[/]", language="[bold"),
    mk("github", "o/[bold", 8, description="[bold", language="[/]"),
    mk("github", "o/clickme", 7, description="[@click=app.quit]CLICKME[/]", language="[@click=app.quit]x[/]"),
]


def make_evil_app(tmp_path, repos=EVIL, **kw):
    hub = make_hub(FakeProvider("github", list(repos)))
    return RepoHubApp(hub, tmp_path, shelves=[Shelf("[/]shelf", topic="[@click=app.quit]t[/]")],
                      cloner=kw.get("cloner", lambda url, root: tmp_path / "r")), hub


def text_of(widget):
    return str(widget.render())


async def open_first(app, pilot, query="x"):
    app.query_one(Input).focus()
    await pilot.press(*query, "enter")
    await settle(app, pilot)
    app.query_one(DataTable).focus()
    await pilot.press("enter")
    await settle(app, pilot)


async def test_markup_in_results_does_not_crash_or_run_actions(tmp_path):
    app, _ = make_evil_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.query_one(DataTable).row_count == 2  # shelf row with markup renders
        app.query_one(Input).focus()
        await pilot.press("x", "enter")
        await settle(app, pilot)
        table = app.query_one(DataTable)
        assert table.row_count == 3
        table.focus()
        for _ in range(3):
            await pilot.click(DataTable, offset=(40, 2))
            await pilot.click(DataTable, offset=(5, 3))
        await pilot.pause()
        assert app.is_running
        table.move_cursor(row=2)
        await pilot.press("enter")
        await settle(app, pilot)
        assert app.is_running and isinstance(app.screen, DetailScreen)
        assert "[@click=app.quit]CLICKME[/]" in text_of(app.screen.query_one("#meta", Static))
        await pilot.press("escape")
        await pilot.pause()
        assert app.is_running


async def test_status_shows_error_text_literally(tmp_path):
    hub = make_hub(FakeProvider("github", error=ProviderError("github", "[@click=app.quit]boom[/]")))
    app = RepoHubApp(hub, tmp_path, shelves=[])
    async with app.run_test() as pilot:
        app.query_one(Input).focus()
        await pilot.press("x", "enter")
        await settle(app, pilot)
        assert app.is_running
        assert "[@click=app.quit]boom[/]" in text_of(app.query_one("#status", Static))


async def open_detail_with_readme(tmp_path, readme):
    hub = make_hub(FakeProvider("github", [mk("github", "o/r", 5)], readme=readme))
    return RepoHubApp(hub, tmp_path, shelves=[]), hub


async def test_https_link_opens_and_others_are_blocked(tmp_path, monkeypatch):
    app, _ = await open_detail_with_readme(tmp_path, "[a](https://example.com) [b](ssh://h/x) [c](vscode://x)")
    opened = []
    monkeypatch.setattr(RepoHubApp, "open_url", lambda self, url, **kw: opened.append(url))
    async with app.run_test() as pilot:
        await open_first(app, pilot)
        md = app.screen.query_one(Markdown)
        for href in ("https://example.com", "ssh://h/x", "vscode://x", "file:///etc/passwd", "rel/path", "HTTP://UP.example"):
            md.post_message(Markdown.LinkClicked(md, href))
            await pilot.pause()
        assert opened == ["https://example.com", "HTTP://UP.example"]
        assert app.is_running


async def test_detail_failure_keeps_app_running(tmp_path):
    app, hub = make_app(tmp_path)

    async def boom(host, slug):
        raise RuntimeError("kaboom")

    hub.detail = boom
    async with app.run_test() as pilot:
        await open_first(app, pilot, "tui")
        assert app.is_running and isinstance(app.screen, DetailScreen)
        assert "kaboom" in text_of(app.screen.query_one("#meta", Static))


async def test_cloner_oserror_keeps_app_running(tmp_path):
    def bad(url, root):
        raise OSError("disk on fire")

    app, _ = make_app(tmp_path, cloner=bad)
    async with app.run_test() as pilot:
        await open_first(app, pilot, "tui")
        await pilot.press("c")
        await pilot.pause()
        await pilot.press("y")
        await settle(app, pilot)
        assert app.is_running


async def test_search_and_shelf_and_favorites_failures_keep_app_running(tmp_path):
    app, hub = make_app(tmp_path)

    async def boom(*a, **k):
        raise RuntimeError("nope")

    hub.search = boom
    hub.shelf = boom
    hub.refresh_favorites = boom
    async with app.run_test() as pilot:
        await pilot.press("enter")  # shelf row
        await settle(app, pilot)
        assert app.is_running
        app.query_one(Input).focus()
        await pilot.press("x", "enter")
        await settle(app, pilot)
        assert app.is_running
        await pilot.press("ctrl+f")
        await settle(app, pilot)
        assert app.is_running


async def test_favorite_before_load_says_still_loading(tmp_path):
    app, hub = make_app(tmp_path)
    gate = asyncio.Event()
    real = hub.detail

    async def slow(host, slug):
        await gate.wait()
        return await real(host, slug)

    hub.detail = slow
    async with app.run_test() as pilot:
        app.query_one(Input).focus()
        await pilot.press(*"tui", "enter")
        await settle(app, pilot)
        app.query_one(DataTable).focus()
        await pilot.press("enter")
        await pilot.pause()
        notes = []
        app.screen.notify = lambda msg, **kw: notes.append(msg)
        await pilot.press("f")
        assert notes == ["Still loading"]
        assert not hub.favorites.is_favorite("github:o/r")
        gate.set()
        await settle(app, pilot)


async def test_favorite_toggle_updates_meta_marker(tmp_path):
    app, hub = make_app(tmp_path)
    async with app.run_test() as pilot:
        await open_first(app, pilot, "tui")
        meta = app.screen.query_one("#meta", Static)
        assert "favorited" not in text_of(meta)
        await pilot.press("f")
        await pilot.pause()
        assert "favorited" in text_of(meta)
        await pilot.press("f")
        await pilot.pause()
        assert "favorited" not in text_of(meta)


async def test_unfavoriting_in_detail_removes_row_from_favorites_view(tmp_path):
    app, hub = make_app(tmp_path)
    hub.favorites.add(mk("github", "o/r", 50))
    async with app.run_test() as pilot:
        await pilot.press("ctrl+f")
        await settle(app, pilot)
        table = app.query_one(DataTable)
        assert table.row_count == 1
        table.focus()
        await pilot.press("enter")
        await settle(app, pilot)
        assert isinstance(app.screen, DetailScreen)
        await pilot.press("f")
        await pilot.pause()
        assert not hub.favorites.is_favorite("github:o/r")
        await pilot.press("escape")
        await pilot.pause()
        assert app.query_one(DataTable).row_count == 0


async def test_home_cancels_inflight_search(tmp_path):
    app, hub = make_app(tmp_path)
    gate = asyncio.Event()
    real = hub.search

    async def slow(q, filters=None):
        await gate.wait()
        return await real(q, filters)

    hub.search = slow
    async with app.run_test() as pilot:
        app.query_one(Input).focus()
        await pilot.press(*"tui", "enter")
        await pilot.pause()
        app.action_home()
        gate.set()
        await pilot.pause()  # (wait_for_complete would raise WorkerCancelled for the cancelled worker)
        await pilot.pause()
        table = app.query_one(DataTable)
        assert table.row_count == 2 and [c.label.plain for c in table.columns.values()] == ["Shelf", "Topic"]


async def test_favorites_view_shows_stored_rows_before_refresh_finishes(tmp_path):
    app, hub = make_app(tmp_path)
    hub.favorites.add(mk("github", "o/r", 50))
    gate = asyncio.Event()

    async def slow_refresh(*a, **k):
        await gate.wait()
        hub.favorites.update(mk("github", "o/r", 99))

    hub.refresh_favorites = slow_refresh
    async with app.run_test() as pilot:
        await pilot.press("ctrl+f")
        await pilot.pause()
        await pilot.pause()
        table = app.query_one(DataTable)
        assert app.view == "favorites" and table.row_count == 1
        assert table.get_row_at(0)[2].plain == "50"
        gate.set()
        await settle(app, pilot)
        assert app.query_one(DataTable).get_row_at(0)[2].plain == "99"


async def test_late_favorites_refresh_does_not_overwrite_other_view(tmp_path):
    app, hub = make_app(tmp_path)
    hub.favorites.add(mk("github", "o/r", 50))
    gate = asyncio.Event()

    async def slow_refresh(*a, **k):
        await gate.wait()

    hub.refresh_favorites = slow_refresh
    async with app.run_test() as pilot:
        await pilot.press("ctrl+f")
        await pilot.pause()
        app.action_home()
        gate.set()
        await pilot.pause()
        await pilot.pause()
        assert app.view == "shelves"
        assert [c.label.plain for c in app.query_one(DataTable).columns.values()] == ["Shelf", "Topic"]


# ---- query syntax ----------------------------------------------------------------------------

def make_gh_app(tmp_path):
    gh = FakeProvider("github", [mk("github", "o/r", 50), mk("github", "p/q", 5)])
    return RepoHubApp(make_hub(gh), tmp_path, shelves=[Shelf("T", topic="tui")]), gh


async def type_query(app, pilot, query):
    app.query_one(Input).value = ""
    app.query_one(Input).focus()
    await pilot.press(*query, "enter")
    await settle(app, pilot)


async def test_query_syntax_reaches_provider(tmp_path):
    app, gh = make_gh_app(tmp_path)
    async with app.run_test() as pilot:
        await type_query(app, pilot, "tui lang:rust nofork sort:forks")
        assert gh.last_query == "tui"
        assert gh.last_filters.language == "rust"
        assert gh.last_filters.hide_forks
        assert gh.last_filters.sort == "forks"
        assert "tui lang:rust nofork sort:forks" in text_of(app.query_one("#status", Static))


async def test_bad_token_reports_ignored_but_still_searches(tmp_path):
    app, gh = make_gh_app(tmp_path)
    async with app.run_test() as pilot:
        await type_query(app, pilot, "tui stars:abc")
        assert app.query_one(DataTable).row_count == 2
        status = text_of(app.query_one("#status", Static))
        assert "Ignored: " in status and "stars" in status


async def test_syntax_only_query_still_runs(tmp_path):
    app, gh = make_gh_app(tmp_path)
    async with app.run_test() as pilot:
        await type_query(app, pilot, "nofork")
        assert gh.calls == 1 and gh.last_query == "" and gh.last_filters.hide_forks
        assert app.query_one(DataTable).row_count == 2


async def test_placeholder_documents_syntax(tmp_path):
    app, _ = make_gh_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.query_one(Input).placeholder == (
            "Search repositories (e.g. tui lang:rust stars:500 days:90 host:codeberg sort:updated nofork)")


async def test_hostile_problem_text_is_shown_literally(tmp_path):
    app, gh = make_gh_app(tmp_path)
    async with app.run_test() as pilot:
        for q in ("stars:[/]", "lang:[@click=app.quit]x[/]"):
            await type_query(app, pilot, q)
            status = text_of(app.query_one("#status", Static))
            assert app.is_running
            assert "Ignored" in status
            assert f"Results for '{q}'" in status
        assert "[@click=app.quit]" in status and "[/]" in status
        assert app.query_one(DataTable).row_count == 2


# ---- notifications never parse dynamic text as markup ---------------------------------------

def record_notify(app):
    calls = []
    orig = app.notify

    def spy(message, **kw):
        calls.append((message, kw))
        return orig(message, **kw)

    app.notify = spy
    return calls


async def test_clone_error_notification_is_plain_text(tmp_path):
    def bad(url, root):
        raise CloneError("fatal: [remote rejected] [/] [@click=app.quit]x[/]")

    app, _ = make_app(tmp_path, cloner=bad)
    calls = record_notify(app)
    async with app.run_test() as pilot:
        await open_first(app, pilot, "tui")
        await pilot.press("c")
        await pilot.pause()
        await pilot.press("y")
        await settle(app, pilot)
        await pilot.pause()
        assert app.is_running
        assert any("[remote rejected]" in m for m, _ in calls)
        assert all(kw.get("markup") is False for _, kw in calls)


async def test_failing_search_notification_is_plain_text(tmp_path):
    hub = make_hub(FakeProvider("github"))

    async def boom(*a, **k):
        raise RuntimeError("[/] [bold")

    hub.search = boom
    app = RepoHubApp(hub, tmp_path, shelves=[])
    calls = record_notify(app)
    async with app.run_test() as pilot:
        app.query_one(Input).focus()
        await pilot.press("x", "enter")
        await settle(app, pilot)
        await pilot.pause()
        assert app.is_running
        assert calls and all(kw.get("markup") is False for _, kw in calls)


async def test_favorites_error_notification_is_plain_text(tmp_path):
    app, hub = make_app(tmp_path)

    async def boom():
        raise RuntimeError("[/] [bold")

    hub.refresh_favorites = boom
    calls = record_notify(app)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+f")
        await settle(app, pilot)
        await pilot.pause()
        assert app.is_running
        assert calls and all(kw.get("markup") is False for _, kw in calls)


async def test_successful_clone_with_markup_path_is_plain_text(tmp_path):
    app, _ = make_app(tmp_path, cloner=lambda url, root: tmp_path / "[/][@click=app.quit]x[/]")
    calls = record_notify(app)
    async with app.run_test() as pilot:
        await open_first(app, pilot, "tui")
        await pilot.press("c")
        await pilot.pause()
        await pilot.press("y")
        await settle(app, pilot)
        await pilot.pause()
        assert app.is_running
        assert any("Cloned to" in m for m, _ in calls)
        assert all(kw.get("markup") is False for _, kw in calls)


# ---- curated shelves (Task 11) ---------------------------------------------------------------

from repohub.core.browse import LoadedShelves, ShelfEntry, Snapshot  # noqa: E402


def _cur_entries(n, note=lambda i: f"note{i}"):
    return tuple(ShelfEntry("github", f"o/r{i}", note(i), Snapshot(f"snapdesc{i}", 100 + i, "Go", "MIT", "2026-08-01"))
                 for i in range(n))


def make_cur_app(tmp_path, n=30, name="Curated one", provider=None, **kw):
    gh = provider or FakeProvider("github", [mk("github", f"o/r{i}", 999, description="livedesc") for i in range(n)])
    shelf = Shelf(name=name, repos=_cur_entries(n, **kw), as_of="2026-09-01")
    return RepoHubApp(make_hub(gh), tmp_path, shelves=[shelf]), gh


async def open_shelf_row(app, pilot):
    app.query_one(DataTable).focus()
    await pilot.press("enter")
    await settle(app, pilot)


def col(table, i):
    return [table.get_row_at(r)[i].plain for r in range(table.row_count)]


async def test_shelf_list_shows_curated_count(tmp_path):
    app, _ = make_cur_app(tmp_path, 30)
    async with app.run_test() as pilot:
        await pilot.pause()
        row = app.query_one(DataTable).get_row_at(0)
        assert row[1].plain == "curated · 30"


async def test_curated_shelf_pages_with_brackets(tmp_path):
    app, _ = make_cur_app(tmp_path, 30)
    async with app.run_test() as pilot:
        await pilot.pause()
        await open_shelf_row(app, pilot)
        table = app.query_one(DataTable)
        assert app.view == "shelf" and table.row_count == 25
        assert [c.label.plain for c in table.columns.values()] == ["Repo", "Host", "Stars", "Lang", "Note"]
        assert "Curated one: 1-25 of 30  (as of 2026-09-01)" in text_of(app.query_one("#status", Static))
        await pilot.press("]")
        await settle(app, pilot)
        assert table.row_count == 5 and table.get_row_at(0)[0].plain == "o/r25"
        assert "26-30 of 30" in text_of(app.query_one("#status", Static))
        await pilot.press("]")  # last page: nothing
        await settle(app, pilot)
        assert table.row_count == 5 and app.shelf_offset == 25
        await pilot.press("[")
        await settle(app, pilot)
        assert table.row_count == 25 and app.shelf_offset == 0
        await pilot.press("[")  # first page: nothing
        await settle(app, pilot)
        assert table.row_count == 25 and app.shelf_offset == 0


async def test_paging_keys_do_nothing_outside_curated_shelf(tmp_path):
    app, _ = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("]", "[")
        await settle(app, pilot)
        assert app.view == "shelves" and app.query_one(DataTable).row_count == 2
        await open_shelf_row(app, pilot)  # search shelf
        n = app.query_one(DataTable).row_count
        await pilot.press("]", "[")
        await settle(app, pilot)
        assert app.query_one(DataTable).row_count == n and app.view == "results"


async def test_curated_note_falls_back_to_description(tmp_path):
    app, _ = make_cur_app(tmp_path, 2, note=lambda i: "" if i == 0 else "mynote")
    async with app.run_test() as pilot:
        await pilot.pause()
        await open_shelf_row(app, pilot)
        assert col(app.query_one(DataTable), 4) == ["livedesc", "mynote"]


async def test_curated_hostile_text_renders_literally(tmp_path):
    hostile = ["[/]", "[@click=app.quit]x[/]", "[bold"]
    app, _ = make_cur_app(tmp_path, 3, name="[@click=app.quit]n[/][bold", note=lambda i: hostile[i])
    async with app.run_test() as pilot:
        await pilot.pause()
        await open_shelf_row(app, pilot)
        table = app.query_one(DataTable)
        assert col(table, 4) == hostile
        table.focus()
        for _ in range(3):
            await pilot.click(DataTable, offset=(40, 2))
            await pilot.click(DataTable, offset=(60, 3))
        await pilot.pause()
        assert app.is_running
        assert "[@click=app.quit]n[/][bold: 1-3 of 3" in text_of(app.query_one("#status", Static))


async def test_curated_refresh_failure_keeps_snapshot_and_shows_error(tmp_path):
    gh = FakeProvider("github", [mk("github", "o/r0", 1)], detail_error=ProviderError("github", "[@click=app.quit]rate[/]"))
    app, _ = make_cur_app(tmp_path, 3, provider=gh)
    async with app.run_test() as pilot:
        await pilot.pause()
        await open_shelf_row(app, pilot)
        table = app.query_one(DataTable)
        assert app.is_running and table.row_count == 3
        assert col(table, 2) == ["100", "101", "102"]
        assert col(table, 4) == ["note0", "note1", "note2"]
        assert "github: " in text_of(app.query_one("#status", Static))
        assert "[@click=app.quit]rate[/]" in text_of(app.query_one("#status", Static))


async def test_curated_provider_runtime_error_does_not_crash(tmp_path):
    gh = FakeProvider("github", [mk("github", "o/r0", 1)], detail_error=RuntimeError("[/] kaboom"))
    app, _ = make_cur_app(tmp_path, 3, provider=gh)
    async with app.run_test() as pilot:
        await pilot.pause()
        await open_shelf_row(app, pilot)
        assert app.is_running and app.query_one(DataTable).row_count == 3


async def test_curated_page_failure_is_reported_plainly(tmp_path):
    app, _ = make_cur_app(tmp_path, 3)
    hub = app.hub
    calls = record_notify(app)

    async def boom(*a, **k):
        raise RuntimeError("[/] [bold")

    hub.curated_page = boom
    async with app.run_test() as pilot:
        await pilot.pause()
        await open_shelf_row(app, pilot)
        await pilot.pause()
        assert app.is_running and "[/] [bold" in text_of(app.query_one("#status", Static))
        assert calls and all(kw.get("markup") is False for _, kw in calls)


async def test_escape_returns_to_shelf_list_and_late_result_is_dropped(tmp_path):
    app, _ = make_cur_app(tmp_path, 30)
    hub = app.hub
    gate = asyncio.Event()
    real = hub.curated_page

    async def slow(*a, **k):
        await gate.wait()
        return await real(*a, **k)

    hub.curated_page = slow
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one(DataTable).focus()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("escape")
        gate.set()
        await pilot.pause()
        await pilot.pause()
        assert app.view == "shelves"
        assert [c.label.plain for c in app.query_one(DataTable).columns.values()] == ["Shelf", "Topic"]
    app2, _ = make_cur_app(tmp_path, 30)
    async with app2.run_test() as pilot:
        await pilot.pause()
        await open_shelf_row(app2, pilot)
        await pilot.press("escape")
        await pilot.pause()
        assert app2.view == "shelves" and app2.query_one(DataTable).row_count == 2


async def test_selecting_curated_row_opens_detail(tmp_path):
    app, _ = make_cur_app(tmp_path, 3)
    async with app.run_test() as pilot:
        await pilot.pause()
        await open_shelf_row(app, pilot)
        app.query_one(DataTable).focus()
        app.query_one(DataTable).move_cursor(row=1)
        await pilot.press("enter")
        await settle(app, pilot)
        assert isinstance(app.screen, DetailScreen) and app.screen.detail.repo.slug == "o/r1"
        await pilot.press("escape")
        await pilot.pause()
        assert app.view == "shelf" and app.query_one(DataTable).row_count == 3


async def test_load_all_shelves_used_by_default_and_problems_shown(tmp_path, monkeypatch):
    shelf = Shelf(name="S", repos=_cur_entries(2), as_of="2026-09-01")
    monkeypatch.setattr("repohub.tui.app.load_all_shelves",
                        lambda: LoadedShelves([shelf], ["bad [/] file", "second"]))
    app = RepoHubApp(make_hub(FakeProvider("github")), tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.shelves == [shelf] and app.shelf_problems == ["bad [/] file", "second"]
        status = text_of(app.query_one("#status", Static))
        assert "2 shelf file problem(s): bad [/] file" in status
        assert app.query_one(DataTable).row_count == 2


# ---- Phase 2: codeberg and extra hosts -----------------------------------------------------------

def make_cb_app(tmp_path, cloner=None, hub=None):
    cb = FakeProvider("codeberg", [mk("codeberg", "o/r", 7)])
    hub = hub or make_hub(cb)
    return RepoHubApp(hub, tmp_path, shelves=[Shelf("T", topic="tui")],
                      cloner=cloner or (lambda url, root: tmp_path / "r")), hub


async def test_codeberg_detail_favorite_and_clone(tmp_path):
    calls = []
    app, hub = make_cb_app(tmp_path, cloner=lambda url, root: calls.append(url) or tmp_path / "r")
    async with app.run_test() as pilot:
        await type_query(app, pilot, "tui host:codeberg")
        await settle(app, pilot)
        assert app.query_one(DataTable).row_count == 1
        app.query_one(DataTable).focus()
        await pilot.press("enter")
        await settle(app, pilot)
        assert isinstance(app.screen, DetailScreen) and app.screen.detail.repo.host == "codeberg"
        await pilot.press("f")
        await pilot.pause()
        assert hub.favorites.is_favorite("codeberg:o/r")
        await pilot.press("c")
        await pilot.pause()
        await pilot.press("y")
        await settle(app, pilot)
        assert calls == ["https://codeberg.org/o/r.git"]


async def test_extra_host_rows_work(tmp_path):
    from repohub.core.hosts import BUILTIN_HOSTS, HostRegistry, HostSpec, set_registry

    set_registry(HostRegistry(BUILTIN_HOSTS + (
        HostSpec("myforge", "forgejo", "MyForge", "git.example.org", "https://git.example.org/api/v1",
                 ("REPOHUB_MYFORGE_TOKEN",), False),)))
    calls = []
    hub = make_hub(FakeProvider("myforge", [mk("myforge", "o/r")]))
    app, _ = make_cb_app(tmp_path, cloner=lambda url, root: calls.append(url) or tmp_path / "r", hub=hub)
    async with app.run_test() as pilot:
        await type_query(app, pilot, "tui")
        await settle(app, pilot)
        app.query_one(DataTable).focus()
        await pilot.press("enter")
        await settle(app, pilot)
        assert app.screen.detail.repo.host == "myforge"
        await pilot.press("c")
        await pilot.pause()
        await pilot.press("y")
        await settle(app, pilot)
        assert calls == ["https://git.example.org/o/r.git"]


async def test_favorite_row_of_unconfigured_host_shows_clean_error(tmp_path):
    from repohub.core.models import Repo

    hub = make_hub(FakeProvider("github"))
    hub.favorites.add(mk("gone", "o/r"))
    app, _ = make_cb_app(tmp_path, hub=hub)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+f")
        await settle(app, pilot)
        assert app.query_one(DataTable).row_count == 1
        app.query_one(DataTable).focus()
        await pilot.press("enter")
        await settle(app, pilot)
        assert isinstance(app.screen, DetailScreen) and app.screen.detail is None
        assert "host not configured" in text_of(app.screen.query_one("#meta", Static))
        await pilot.press("c")
        await pilot.pause()
        assert app.is_running and not isinstance(app.screen, type(None))


async def test_host_problems_shown_plain_on_shelf_list(tmp_path):
    hostile = "hosts file: bad\x1b[31m [/] [@click=app.quit]x[/] \u202e " + "z" * 500
    hub = make_hub(FakeProvider("github"))
    hub.host_problems = [hostile, "second"]
    app, _ = make_cb_app(tmp_path, hub=hub)
    async with app.run_test() as pilot:
        await pilot.pause()
        status = text_of(app.query_one("#status", Static))
        assert app.is_running
        assert "2 host problem(s)" in status and "[@click=app.quit]x[/]" in status
        assert "\x1b" not in status and "\u202e" not in status
        assert len(status) < 500
        await pilot.press("enter")  # keys still work; the app was not quit by the markup
        await pilot.pause()
        assert app.is_running


async def test_host_problems_sit_next_to_shelf_problems(tmp_path, monkeypatch):
    shelf = Shelf(name="S", repos=_cur_entries(1), as_of="2026-09-01")
    monkeypatch.setattr("repohub.tui.app.load_all_shelves", lambda: LoadedShelves([shelf], ["shelf bad"]))
    hub = make_hub(FakeProvider("github"))
    hub.host_problems = ["host bad"]
    app = RepoHubApp(hub, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        status = text_of(app.query_one("#status", Static))
        assert "shelf bad" in status and "host bad" in status


async def test_unconfigured_host_shelf_status_shows_plain_message(tmp_path):
    from repohub.core.browse import ShelfEntry, Snapshot
    shelf = Shelf("Gone", repos=(ShelfEntry("gone", "o/r", "n", Snapshot("snap", 3)),), as_of="2026-09-01")
    app = RepoHubApp(make_hub(FakeProvider("github")), tmp_path, shelves=[shelf],
                     cloner=lambda url, root: tmp_path / "r")
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one(DataTable).focus()
        await pilot.press("enter")
        await settle(app, pilot)
        assert "gone: host not configured" in text_of(app.query_one("#status"))


async def test_d_removes_selected_favorite_without_opening_it_even_for_unconfigured_host(tmp_path):
    app, hub = make_app(tmp_path)
    hub.favorites.add(mk("oldforge", "o/gone", 7))
    hub.favorites.add(mk("github", "o/r", 50))
    async with app.run_test() as pilot:
        await pilot.press("ctrl+f")
        await settle(app, pilot)
        table = app.query_one(DataTable)
        assert table.row_count == 2
        table.focus()
        keys = [r.value for r in table.rows]
        idx = keys.index("repo:oldforge:o/gone")
        table.move_cursor(row=idx)
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()
        assert not hub.favorites.is_favorite("oldforge:o/gone") and hub.favorites.is_favorite("github:o/r")
        assert app.query_one(DataTable).row_count == 1
        assert not isinstance(app.screen, DetailScreen)


async def test_d_outside_favorites_does_nothing(tmp_path):
    app, hub = make_app(tmp_path)
    hub.favorites.add(mk("github", "o/r", 50))
    async with app.run_test() as pilot:
        await settle(app, pilot)
        app.query_one(DataTable).focus()
        await pilot.press("d")
        await pilot.pause()
        assert hub.favorites.is_favorite("github:o/r")
