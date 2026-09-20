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
        assert app.query_one(DataTable).row_count == 1


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
        assert app.query_one(DataTable).row_count == 1  # shelf row with markup renders
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
        assert table.row_count == 1 and [c.label.plain for c in table.columns.values()] == ["Shelf", "Topic"]
