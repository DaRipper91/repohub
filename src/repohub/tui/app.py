from __future__ import annotations

import asyncio
from urllib.parse import urlparse

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen, Screen
from textual.widgets import DataTable, Footer, Header, Input, Markdown, Static

from repohub.core.browse import load_shelves
from repohub.core.clone import CloneError, clone as do_clone, clone_url, plan_clone
from repohub.core.providers.base import ProviderError


class ConfirmClone(ModalScreen[bool]):
    BINDINGS = [Binding("y", "confirm", "Yes"), Binding("n", "cancel", "No"), Binding("escape", "cancel", "No")]

    def __init__(self, target):
        super().__init__()
        self.target = target

    def compose(self) -> ComposeResult:
        yield Static(f"Clone into:\n{self.target}\n\ny = confirm    n = cancel", markup=False)

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class DetailScreen(Screen):
    BINDINGS = [Binding("escape", "back", "Back"), Binding("f", "favorite", "Favorite"),
                Binding("c", "clone", "Clone")]

    def __init__(self, hub, host: str, slug: str, clone_root, cloner):
        super().__init__()
        self.hub, self.host, self.slug, self.clone_root, self.cloner = hub, host, slug, clone_root, cloner
        self.detail = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Loading…", id="meta", markup=False)
        yield Markdown("", id="readme", open_links=False)
        yield Footer()

    def on_mount(self) -> None:
        self.run_worker(self._load(), exclusive=True)

    async def _load(self) -> None:
        meta = self.query_one("#meta", Static)
        try:
            self.detail = await self.hub.detail(self.host, self.slug)
            self._render_meta()
            await self.query_one("#readme", Markdown).update(self.detail.readme or "_No README._")
        except Exception as e:  # never let a worker crash take the app down
            self.detail = None
            meta.update(f"Could not load {self.slug}: {e}")

    def _render_meta(self) -> None:
        r, rel = self.detail.repo, self.detail.release
        release = f"{rel.tag}{' (arm64 available)' if rel.has_arm64 else ''}" if rel else "no releases"
        star = "★ favorited" if self.hub.favorites.is_favorite(r.key) else ""
        self.query_one("#meta", Static).update(
            f"{r.slug} [{r.host}]  ★ {r.stars}  {r.language or 'n/a'}  {r.license or 'no license'}  {star}\n"
            f"{r.description}\nRelease: {release}")

    def action_back(self) -> None:
        self.app.pop_screen()
        self.app.refresh_current_view()

    def on_markdown_link_clicked(self, event: Markdown.LinkClicked) -> None:
        event.stop()
        try:
            scheme = urlparse(event.href).scheme.lower()
        except ValueError:
            scheme = ""
        if scheme in ("http", "https"):
            self.app.open_url(event.href)
        else:
            self.notify("Blocked non-web link", severity="warning")

    def action_favorite(self) -> None:
        if self.detail is None:
            self.notify("Still loading")
            return
        key = self.detail.repo.key
        try:
            if self.hub.favorites.is_favorite(key):
                self.hub.favorites.remove(key)
                self.notify("Removed from favorites")
            else:
                self.hub.favorites.add(self.detail.repo)
                self.notify("Added to favorites")
        except Exception as e:
            self.notify(f"Could not update favorites: {e}", severity="error")
            return
        self._render_meta()

    def action_clone(self) -> None:
        try:
            url = clone_url(self.host, self.slug)
            target = plan_clone(url, self.clone_root)
        except CloneError as e:
            self.notify(str(e), severity="error")
            return

        def done(confirmed: bool | None) -> None:
            if confirmed:
                self.run_worker(self._run_clone(url), exclusive=False)

        self.app.push_screen(ConfirmClone(target), done)

    async def _run_clone(self, url: str) -> None:
        try:
            target = await asyncio.to_thread(self.cloner, url, self.clone_root)
        except Exception as e:
            self.notify(f"Clone failed: {e}", severity="error")
        else:
            self.notify(f"Cloned to {target}")


class RepoHubApp(App):
    TITLE = "RepoHub"
    BINDINGS = [Binding("ctrl+f", "favorites", "Favorites"), Binding("escape", "home", "Shelves")]

    def __init__(self, hub, clone_root, shelves=None, cloner=do_clone):
        super().__init__()
        self.hub, self.clone_root, self.cloner = hub, clone_root, cloner
        self.shelves = load_shelves() if shelves is None else shelves
        self.view = "shelves"

    def compose(self) -> ComposeResult:
        yield Header()
        yield Input(placeholder="Search GitHub and GitLab, then press Enter", id="q")
        yield Static("", id="status", markup=False)
        yield DataTable(cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        self.action_home()

    def _table(self) -> DataTable:
        return self.query_one(DataTable)

    def _status(self, text: str) -> None:
        self.query_one("#status", Static).update(text)

    def action_home(self) -> None:
        self.workers.cancel_node(self)  # a late search/shelf/favorites result must not overwrite the shelves
        self.view = "shelves"
        t = self._table()
        t.clear(columns=True)
        t.add_columns("Shelf", "Topic")
        for i, s in enumerate(self.shelves):
            t.add_row(Text(s.name), Text(s.topic or s.query), key=f"shelf:{i}")
        self._status("Shelves. Press Enter on one, or type a search above.")

    def action_favorites(self) -> None:
        self.run_worker(self._show_favorites(), exclusive=True)

    async def _show_favorites(self) -> None:
        try:
            await self.hub.refresh_favorites()
        except Exception as e:
            self.notify(f"Could not refresh favorites: {e}", severity="error")
        self._show_repos(self.hub.favorites.list(), "Favorites", view="favorites")

    def refresh_current_view(self) -> None:
        """Re-render the favorites table from the store (no network), e.g. after unfavoriting."""
        if self.view == "favorites":
            self._show_repos(self.hub.favorites.list(), "Favorites", view="favorites")

    def _show_repos(self, repos, status: str, view: str = "results") -> None:
        self.view = view
        t = self._table()
        t.clear(columns=True)
        t.add_columns("Repo", "Host", "Stars", "Lang", "Description")
        seen: set[str] = set()
        for r in repos:
            key = f"repo:{r.host}:{r.slug}"
            if key in seen:
                continue
            seen.add(key)
            # Text objects are not markup-parsed, so untrusted values cannot inject markup or actions.
            t.add_row(Text(r.slug), Text(r.host), Text(str(r.stars)), Text(r.language or "n/a"),
                      Text(r.description[:70]), key=key)
        self._status(status)

    def _show_result(self, result, label: str) -> None:
        notes = [label]
        if result.stale:
            notes.append("(cached)")
        notes += [f"{h}: {m}" for h, m in result.errors.items()]
        self._show_repos(result.repos, "  ".join(notes))

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        query = event.value.strip()
        if query:
            self._status("Searching…")
            self.run_worker(self._search(query), exclusive=True)

    async def _search(self, query: str) -> None:
        try:
            result = await self.hub.search(query)
        except Exception as e:
            self._status(f"Search failed: {e}")
            self.notify(f"Search failed: {e}", severity="error")
            return
        self._show_result(result, f"Results for '{query}'")

    async def _open_shelf(self, index: int) -> None:
        shelf = self.shelves[index]
        try:
            result = await self.hub.shelf(shelf)
        except Exception as e:
            self._status(f"Could not load shelf: {e}")
            self.notify(f"Could not load shelf: {e}", severity="error")
            return
        self._show_result(result, shelf.name)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        key = event.row_key.value or ""
        if key.startswith("shelf:"):
            self._status("Loading shelf…")
            self.run_worker(self._open_shelf(int(key.split(":")[1])), exclusive=True)
        elif key.startswith("repo:"):
            _, host, slug = key.split(":", 2)
            self.push_screen(DetailScreen(self.hub, host, slug, self.clone_root, self.cloner))


def main() -> None:
    from repohub.config import build_hub, clone_root

    RepoHubApp(build_hub(), clone_root()).run()
