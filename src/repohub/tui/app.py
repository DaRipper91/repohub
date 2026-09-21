from __future__ import annotations

import asyncio
from urllib.parse import urlparse

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen, Screen
from textual.widgets import DataTable, Footer, Header, Input, Markdown, Static

from repohub.core.browse import load_all_shelves
from repohub.core.clone import CloneError, clone as do_clone, clone_url, plan_clone
from repohub.core.providers.base import ProviderError
from repohub.core.queryparse import parse_query
from repohub.core.textsafe import clean_text


PAGE = 25
MAX_HOST_PROBLEM = 160  # characters of the first host problem shown in the status line


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


class ConfirmWrite(ModalScreen[bool]):
    """y/n confirmation for an action that changes the user's account. Text is shown as plain text."""
    BINDINGS = [Binding("y", "confirm", "Yes"), Binding("n", "cancel", "No"), Binding("escape", "cancel", "No")]

    def __init__(self, text: str):
        super().__init__()
        self.text = text

    def compose(self) -> ComposeResult:
        yield Static(f"{self.text}\n\ny = confirm    n = cancel", markup=False)

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class DetailScreen(Screen):
    BINDINGS = [Binding("escape", "back", "Back"), Binding("f", "favorite", "Favorite"),
                Binding("c", "clone", "Clone"), Binding("s", "star", "Star/unstar"), Binding("k", "fork", "Fork")]

    def __init__(self, hub, host: str, slug: str, clone_root, cloner):
        super().__init__()
        self.hub, self.host, self.slug, self.clone_root, self.cloner = hub, host, slug, clone_root, cloner
        self.detail = None
        self._writing = False  # one star/fork flow at a time: no stacked confirmations, no double sends

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
            self.hub.record_view(self.detail.repo)  # only stored while the opt-in history is on
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
            self.notify("Blocked non-web link", severity="warning", markup=False)

    def action_favorite(self) -> None:
        if self.detail is None:
            self.notify("Still loading", markup=False)
            return
        key = self.detail.repo.key
        try:
            if self.hub.favorites.is_favorite(key):
                self.hub.favorites.remove(key)
                self.notify("Removed from favorites", markup=False)
            else:
                self.hub.favorites.add(self.detail.repo)
                self.notify("Added to favorites", markup=False)
        except Exception as e:
            self.notify(f"Could not update favorites: {e}", severity="error", markup=False)
            return
        self._render_meta()

    def action_star(self) -> None:
        if self.detail is None:
            self.notify("Still loading", markup=False)
            return
        self.run_worker(self._write("star"), exclusive=False, group="write")

    def action_fork(self) -> None:
        if self.detail is None:
            self.notify("Still loading", markup=False)
            return
        self.run_worker(self._write("fork"), exclusive=False, group="write")

    async def _write(self, action: str) -> None:
        """Ask first (naming host, repository and account), then make exactly one attempt."""
        if self._writing:
            return
        self._writing = True
        try:
            acct = await self.hub.account(self.host)
            if acct.status != "signed in":
                self._writing = False
                self.notify(f"Not signed in on {self.host}: press F2 on the main screen", severity="warning", markup=False)
                return
            if action == "star":
                on = await self.hub.starred(self.host, self.slug)
                action = "unstar" if on else "star"
        except Exception as e:
            self._writing = False
            self.notify(f"Could not check the account: {e}", severity="error", markup=False)
            return
        what = {"star": "Star", "unstar": "Unstar", "fork": "Fork"}[action]
        text = f"{what} {self.slug}\nHost: {self.host}\nAccount: {acct.login or 'unknown'}"
        if action == "fork":
            text += "\nThis creates a repository in your account. RepoHub never deletes it."

        def done(confirmed: bool | None) -> None:
            if confirmed:
                self.run_worker(self._do_write(action), exclusive=False, group="write")
            else:
                self._writing = False

        self.app.push_screen(ConfirmWrite(text), done)

    async def _do_write(self, action: str) -> None:
        try:
            if action == "fork":
                res = await self.hub.fork(self.host, self.slug)
                self.notify(f"Forked to {res.slug}", markup=False)
            else:
                await self.hub.set_star(self.host, self.slug, action == "star")
                self.notify("Starred" if action == "star" else "Unstarred", markup=False)
        except Exception as e:  # ProviderError text is already short and sanitised; one attempt, no retry
            self.notify(f"{action} failed: {e}", severity="error", markup=False)
        finally:
            self._writing = False

    def action_clone(self) -> None:
        try:
            url = clone_url(self.host, self.slug)
            target = plan_clone(url, self.clone_root)
        except CloneError as e:
            self.notify(str(e), severity="error", markup=False)
            return

        def done(confirmed: bool | None) -> None:
            if confirmed:
                self.run_worker(self._run_clone(url), exclusive=False)

        self.app.push_screen(ConfirmClone(target), done)

    async def _run_clone(self, url: str) -> None:
        try:
            target = await asyncio.to_thread(self.cloner, url, self.clone_root)
        except Exception as e:
            self.notify(f"Clone failed: {e}", severity="error", markup=False)
        else:
            self.notify(f"Cloned to {target}", markup=False)


class RepoHubApp(App):
    TITLE = "RepoHub"
    BINDINGS = [Binding("ctrl+f", "favorites", "Favorites"), Binding("escape", "home", "Shelves"),
                Binding("]", "next_page", "Next page"), Binding("[", "prev_page", "Prev page"),
                Binding("d", "remove_favorite", "Remove favorite"), Binding("f2", "accounts", "Accounts"), Binding("f3", "history", "History on/off"),
                Binding("f4", "clear_history", "Clear history"), Binding("a", "accounts", "Accounts", show=False)]

    def __init__(self, hub, clone_root, shelves=None, cloner=do_clone):
        super().__init__()
        self.hub, self.clone_root, self.cloner = hub, clone_root, cloner
        if shelves is None:
            loaded = load_all_shelves()
            self.shelves, self.shelf_problems = loaded.shelves, list(loaded.problems)
        else:
            self.shelves, self.shelf_problems = shelves, []
        self.view = "shelves"
        self.shelf_index = 0
        self.shelf_offset = 0
        self.shelf_total = 0

    def compose(self) -> ComposeResult:
        yield Header()
        yield Input(placeholder="Search repositories (e.g. tui lang:rust stars:500 days:90 host:codeberg sort:updated nofork)", id="q")
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
        self.refresh_bindings()
        t = self._table()
        t.clear(columns=True)
        t.add_columns("Shelf", "Topic")
        for i, s in enumerate(self.shelves):
            second = f"curated · {len(s.repos)}" if s.curated else (s.topic or s.query)
            t.add_row(Text(s.name), Text(second), key=f"shelf:{i}")
        t.add_row(Text("Recommended for you"), Text("from your favorites, stars and history"), key="recommended")
        status = "Shelves. Press Enter on one, or type a search above."
        if self.shelf_problems:
            status += f"  {len(self.shelf_problems)} shelf file problem(s): {self.shelf_problems[0]}"
        host_problems = list(getattr(self.hub, "host_problems", None) or [])
        if host_problems:
            first = clean_text(str(host_problems[0]))[:MAX_HOST_PROBLEM]
            status += f"  {len(host_problems)} host problem(s): {first}"
        self._status(status)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in ("clear_history", "history") and len(self.screen_stack) > 1:
            return False
        if action == "accounts" and len(self.screen_stack) > 1:  # not from the detail or confirm screens
            return False
        if action in ("next_page", "prev_page"):
            # only on the main screen, in a curated shelf view, and not at the relevant end
            if self.view != "shelf" or len(self.screen_stack) > 1:
                return False
            if action == "next_page":
                return self.shelf_offset + PAGE < self.shelf_total
            return self.shelf_offset > 0
        return True

    def action_next_page(self) -> None:
        if self.check_action("next_page", ()):
            self._go_page(self.shelf_offset + PAGE)

    def action_prev_page(self) -> None:
        if self.check_action("prev_page", ()):
            self._go_page(max(0, self.shelf_offset - PAGE))

    def _go_page(self, offset: int) -> None:
        self._status("Loading page…")
        self.run_worker(self._open_shelf(self.shelf_index, offset), exclusive=True)

    def action_favorites(self) -> None:
        self.run_worker(self._show_favorites(), exclusive=True)

    async def _show_favorites(self) -> None:
        self._show_repos(self.hub.favorites.list(), "Favorites", view="favorites")
        try:
            await self.hub.refresh_favorites()
        except Exception as e:
            self.notify(f"Could not refresh favorites: {e}", severity="error", markup=False)
            return
        if self.view == "favorites":
            self._show_repos(self.hub.favorites.list(), "Favorites", view="favorites")

    async def _show_recommended(self) -> None:
        try:
            result = await self.hub.recommend()
        except Exception as e:
            self._status(f"Could not build recommendations: {e}")
            self.notify(f"Could not build recommendations: {e}", severity="error", markup=False)
            return
        self.view = "recommended"
        self.refresh_bindings()
        t = self._table()
        t.clear(columns=True)
        t.add_columns("Repo", "Host", "Stars", "Lang", "Why")
        for item in result.items:
            r = item.repo
            t.add_row(Text(r.slug), Text(r.host), Text(str(r.stars)), Text(r.language or "n/a"), Text(item.why[:80]),
                      key=f"repo:{r.host}:{r.slug}")
        notes = ["Recommended for you"]
        if not result.signal:
            notes.append("Nothing yet: favorite or star some repositories (F3 turns on history).")
        elif not result.items:
            notes.append("No suggestions found.")
        notes += [f"{h}: {m}" for h, m in result.errors.items()]
        self._status("  ".join(notes))

    def action_history(self) -> None:
        on = not self.hub.history.enabled
        self.hub.history.set_enabled(on)
        self.notify("Local history is on" if on else "Local history is off", markup=False)

    def action_clear_history(self) -> None:
        def done(confirmed: bool | None) -> None:
            if confirmed:
                self.hub.history.clear()
                self.notify("History cleared", markup=False)

        self.push_screen(ConfirmWrite("Clear your local history? This cannot be undone."), done)

    def action_accounts(self) -> None:
        self._status("Checking accounts…")
        self.run_worker(self._show_accounts(), exclusive=True)

    async def _show_accounts(self) -> None:
        try:
            infos = await self.hub.accounts()
        except Exception as e:
            self._status(f"Could not load accounts: {e}")
            self.notify(f"Could not load accounts: {e}", severity="error", markup=False)
            return
        self.view = "accounts"
        self.refresh_bindings()
        t = self._table()
        t.clear(columns=True)
        t.add_columns("Host", "Status", "Account", "Token from", "Scopes", "Rate left", "Star/fork")
        for i in infos:
            scopes = "not reported" if i.scopes is None else (", ".join(i.scopes) or "none")
            rate = f"{i.rate.remaining}/{i.rate.limit}" if i.rate else "n/a"
            t.add_row(Text(i.host), Text(i.status), Text(i.login or "-"), Text(i.source or "-"),
                      Text(scopes), Text(rate), Text(i.can_star_fork), key=f"account:{i.host}")
        notes = [f"{i.host}: {i.message or i.hint}" for i in infos if i.message or i.hint]
        recent = self.hub.recent_actions(3)
        notes += [f"{'ok' if e.ok else 'failed'} {e.action} {e.slug} on {e.host}" for e in recent]
        self._status("Accounts (nothing is stored).  " + "  ".join(notes))

    def action_remove_favorite(self) -> None:
        """Favorites view only: remove the selected row without opening it (works for unconfigured hosts)."""
        if self.view != "favorites":
            return
        t = self._table()
        if t.row_count == 0:
            return
        try:
            key = t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value or ""
        except Exception:
            return
        if not key.startswith("repo:"):
            return
        _, host, slug = key.split(":", 2)
        self.hub.favorites.remove(f"{host}:{slug.lower()}")
        self.notify("Removed from favorites", markup=False)
        self.refresh_current_view()

    def refresh_current_view(self) -> None:
        """Re-render the favorites table from the store (no network), e.g. after unfavoriting."""
        if self.view == "favorites":
            self._show_repos(self.hub.favorites.list(), "Favorites", view="favorites")

    def _show_repos(self, repos, status: str, view: str = "results") -> None:
        self.view = view
        self.refresh_bindings()
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
            parsed = parse_query(query)
            result = await self.hub.search(parsed.text, parsed.filters)
        except Exception as e:
            self._status(f"Search failed: {e}")
            self.notify(f"Search failed: {e}", severity="error", markup=False)
            return
        label = f"Results for '{query}'"
        if parsed.problems:
            label += "  Ignored: " + "; ".join(parsed.problems)  # plain-text status Static, never markup
        self._show_result(result, label)

    async def _open_shelf(self, index: int, offset: int = 0) -> None:
        shelf = self.shelves[index]
        if shelf.curated:
            await self._open_curated(index, offset)
            return
        try:
            result = await self.hub.shelf(shelf)
        except Exception as e:
            self._status(f"Could not load shelf: {e}")
            self.notify(f"Could not load shelf: {e}", severity="error", markup=False)
            return
        self._show_result(result, shelf.name)

    async def _open_curated(self, index: int, offset: int) -> None:
        shelf = self.shelves[index]
        try:
            page = await self.hub.curated_page(shelf, offset, PAGE)
        except Exception as e:
            self._status(f"Could not load shelf: {e}")
            self.notify(f"Could not load shelf: {e}", severity="error", markup=False)
            return
        self.view = "shelf"
        self.shelf_index, self.shelf_offset, self.shelf_total = index, page.offset, page.total
        self.refresh_bindings()
        t = self._table()
        t.clear(columns=True)
        t.add_columns("Repo", "Host", "Stars", "Lang", "Note")
        seen: set[str] = set()
        for item in page.items:
            r = item.repo
            key = f"repo:{r.host}:{r.slug}"
            if key in seen:
                continue
            seen.add(key)
            t.add_row(Text(r.slug), Text(r.host), Text(str(r.stars)), Text(r.language or "n/a"),
                      Text((item.note or r.description)[:70]), key=key)
        first = page.offset + 1 if page.items else 0
        notes = [f"{shelf.name}: {first}-{page.offset + len(page.items)} of {page.total}  (as of {shelf.as_of or 'n/a'})"]
        notes += [f"{h}: {m}" for h, m in page.errors.items()]
        self._status("  ".join(notes))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        key = event.row_key.value or ""
        if key == "recommended":
            self._status("Working out recommendations…")
            self.run_worker(self._show_recommended(), exclusive=True)
        elif key.startswith("shelf:"):
            self._status("Loading shelf…")
            self.run_worker(self._open_shelf(int(key.split(":")[1])), exclusive=True)
        elif key.startswith("repo:"):
            _, host, slug = key.split(":", 2)
            self.push_screen(DetailScreen(self.hub, host, slug, self.clone_root, self.cloner))


def main(argv: list[str] | None = None) -> None:
    import argparse

    from repohub import __version__

    parser = argparse.ArgumentParser(prog="repohub-tui", description="RepoHub terminal app for browsing repositories on GitHub, GitLab, Codeberg and other Forgejo servers")
    parser.add_argument("--version", action="version", version=f"repohub {__version__}")
    parser.parse_args(argv)

    from repohub.config import build_hub, clone_root

    RepoHubApp(build_hub(), clone_root()).run()
