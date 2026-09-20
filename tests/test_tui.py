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
