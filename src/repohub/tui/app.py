from __future__ import annotations

import asyncio
from urllib.parse import urlparse

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen, Screen
from textual.widgets import DataTable, Footer, Header, Input, Markdown, Static

from repohub.core.awareness import Awareness
from repohub.core.browse import load_all_shelves
from repohub.core.runner import RunError, Runner
from repohub.core.runplan import WARNING
from repohub.core.settings import Settings
from repohub.core.roots import BUDGET_SECONDS, ScanRoots, discover, home_start
from repohub.core.clone import CloneError, clone as do_clone, clone_url, plan_clone
from repohub.core.providers.base import ProviderError
from repohub.core.queryparse import parse_query
from repohub.core.textsafe import clean_text


PAGE = 25
SYMBOL = {"ok": "✓", "no": "✗", "warn": "!", "info": "·"}
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


class RunScreen(Screen):
    """Guided install: the fixed steps for a cloned repository; each command needs its own y/n approval."""
    BINDINGS = [Binding("escape", "back", "Back"), Binding("c", "cancel_run", "Cancel run")]

    def __init__(self, runner, host: str, slug: str):
        super().__init__()
        self.runner, self.host, self.slug = runner, host, slug
        self.plan = None
        self.session = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="info", markup=False)
        yield DataTable(cursor_type="row")
        yield Static("", id="log", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        clone, self.plan = self.runner.find(self.host, self.slug)
        info = self.query_one("#info", Static)
        t = self.query_one(DataTable)
        t.add_columns("Step", "Command")
        if clone is None:
            info.update("This repository is not cloned in a known folder. Clone it first (c on its screen).")
            return
        if not self.runner.enabled:
            info.update("Guided install is off. Press F6 on the main screen to turn it on.\n" + WARNING)
            return
        info.update(f"{clone.path}\n{WARNING}\nEnter = review and approve one command.")
        for st in self.plan.steps:
            t.add_row(Text(st.title), Text(st.text() + (f"   ({st.note})" if st.note else "")), key=st.id)
        if not self.plan.steps:
            info.update("No standard build command is known for this project.")
        self.set_interval(0.5, self._tick)

    def action_back(self) -> None:
        self.app.pop_screen()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        step = self.plan.step(event.row_key.value or "") if self.plan else None
        if step is None:
            return
        note = f"\n{step.note}" if step.note else ""
        text = f"Run this command in the cloned folder?\n\n  {step.text()}{note}\n\n{WARNING}"

        def done(ok: bool | None) -> None:
            if not ok:
                return
            try:
                self.session = self.runner.start(self.host, self.slug, step.id, self.plan.digest)
            except RunError as e:
                self.notify(str(e), severity="error", markup=False)

        self.app.push_screen(ConfirmWrite(text), done)

    def action_cancel_run(self) -> None:
        if self.session is not None and self.runner.cancel(self.session.id):
            self.notify("Cancelling…", markup=False)

    def _tick(self) -> None:
        s = self.session
        if s is None:
            return
        tail = s.output[-4000:]
        code = f" (exit {s.exit_code})" if s.exit_code is not None else ""
        self.query_one("#log", Static).update(f"{s.command}: {s.status}{code}\n{tail}")


class DetailScreen(Screen):
    BINDINGS = [Binding("escape", "back", "Back"), Binding("f", "favorite", "Favorite"),
                Binding("c", "clone", "Clone"), Binding("s", "star", "Star/unstar"), Binding("k", "fork", "Fork"),
                Binding("i", "install", "Install/build"), Binding("o", "open_claude", "Open in Claude Code")]

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
        lines = [f"{r.slug} [{r.host}]  ★ {r.stars}  {r.language or 'n/a'}  {r.license or 'no license'}  {star}",
                 r.description, f"Release: {release}"]
        favs = self.hub.favorites
        if favs.is_favorite(r.key):
            tags, colls, note = favs.tags(r.key), favs.collections_by_key().get(r.key, []), favs.note(r.key)
            lines += [x for x in (f"Tags: {', '.join(tags)}" if tags else "",
                                  f"Collections: {', '.join(colls)}" if colls else "",
                                  f"Note: {note}" if note else "") if x]
        aware = getattr(self.app, "awareness", None)
        if aware is not None:
            v = aware.check(r, rel)
            lines.append(f"Can I run this here? {v.level.upper()}: {v.summary}")
            lines += [f"  {SYMBOL[c.status]} {c.label}: {c.detail}" for c in v.checks]
        self.query_one("#meta", Static).update("\n".join(lines))

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

    def action_open_claude(self) -> None:
        """Start Claude Code in the cloned folder, after a y/n. RepoHub is suspended while it runs."""
        import shlex
        import shutil

        r = self.detail.repo if self.detail else None
        clone = self.app.awareness.clone_of(r) if r else None
        if clone is None:
            self.notify("Clone this repository first (c), or add its folder on the Folders view (F5)", markup=False)
            return
        exe = shutil.which("claude")
        if exe is not None:  # never a relative PATH hit or a program shipped inside the repository
            import os

            real = os.path.realpath(exe)
            root = os.path.realpath(clone.path)
            if not os.path.isabs(exe) or real == root or real.startswith(root + os.sep):
                exe = None
        if exe is None:
            self.notify(f"Claude Code was not found on PATH. In a terminal: cd {shlex.quote(clone.path)} && claude", markup=False)
            return

        def done(ok: bool | None) -> None:
            if not ok:
                return
            try:
                from textual.app import SuspendNotSupported
            except ImportError:  # older Textual
                SuspendNotSupported = RuntimeError  # noqa: N806
            try:
                with self.app.suspend():
                    self.app.launcher([exe], cwd=clone.path)
            except SuspendNotSupported:
                self.notify(f"This terminal cannot be suspended. Run: cd {shlex.quote(clone.path)} && claude", markup=False)
            except OSError as e:
                self.notify(f"Could not start Claude Code: {e}", severity="error", markup=False)

        self.app.push_screen(ConfirmWrite(
            f"Open Claude Code in this folder?\n{clone.path}\nRepoHub pauses until you leave Claude Code.\n"
            "The folder may carry its own Claude settings (.claude/, .mcp.json): Claude Code asks before trusting them.",), done)

    def action_install(self) -> None:
        runner = getattr(self.app, "runner", None)
        if runner is None:
            return
        self.app.push_screen(RunScreen(runner, self.host, self.slug))

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
    # One look in both themes: colors come from the theme variables, never from fixed values.
    CSS = """
    ConfirmClone, ConfirmWrite { align: center middle; }
    ConfirmClone > Static, ConfirmWrite > Static {
        width: 76; max-width: 95%; height: auto; padding: 1 2; border: round $primary; background: $panel;
    }
    #q { margin: 0 0 1 0; }
    #status { color: $text-muted; padding: 0 1; height: auto; }
    DataTable { height: 1fr; }
    DataTable > .datatable--header { text-style: bold; background: $panel; }
    #meta { padding: 1 2; height: auto; }
    #info { padding: 0 1 1 1; height: auto; color: $text-muted; }
    #log { padding: 1 2; height: auto; border-top: solid $primary; }
    """
    BINDINGS = [Binding("ctrl+f", "favorites", "Favorites"), Binding("escape", "home", "Shelves"),
                Binding("]", "next_page", "Next page"), Binding("[", "prev_page", "Prev page"),
                Binding("d", "remove_favorite", "Remove favorite"), Binding("r", "releases", "New releases", show=False),
                Binding("m", "mark_seen", "Mark seen", show=False), Binding("f2", "accounts", "Accounts"), Binding("f3", "history", "History on/off"),
                Binding("f4", "clear_history", "Clear history"), Binding("f5", "folders", "Folders"), Binding("f6", "guided", "Guided install on/off"), Binding("f7", "theme", "Light/dark", priority=True),
                Binding("s", "scan_home", "Scan home", show=False), Binding("w", "scan_system", "Scan all", show=False), Binding("a", "accounts", "Accounts", show=False)]

    def __init__(self, hub, clone_root, shelves=None, cloner=do_clone, awareness=None, roots=None, runner=None,
                 launcher=None):
        super().__init__()
        self.hub, self.clone_root, self.cloner = hub, clone_root, cloner
        self.awareness = awareness if awareness is not None else Awareness(clone_root)
        self.roots = roots if roots is not None else ScanRoots()
        self.scan_report = None  # the last folder scan: in memory only
        self._scanning = False
        if launcher is None:
            import subprocess
            launcher = subprocess.run
        self.launcher = launcher
        self.runner = runner if runner is not None else Runner(self.awareness, Settings(), hub.actions)
        self.theme = "textual-light" if self._saved_theme() == "light" else "textual-dark"
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

    def _mark(self, repo) -> str:
        """A dot before repositories that are already in the clone folder."""
        return "● " if repo.key in self.awareness.cloned() else ""

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
        if action in ("clear_history", "history", "folders", "guided") and len(self.screen_stack) > 1:
            return False
        if action in ("releases", "mark_seen"):  # only in the favorites / releases views of the main screen
            return self.view in ("favorites", "releases") and len(self.screen_stack) == 1
        if action in ("scan_home", "scan_system"):  # only on the folders view of the main screen
            return self.view == "folders" and len(self.screen_stack) == 1
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
            t.add_row(Text(self._mark(r) + r.slug), Text(r.host), Text(str(r.stars)), Text(r.language or "n/a"), Text(item.why[:80]),
                      key=f"repo:{r.host}:{r.slug}")
        notes = ["Recommended for you"]
        if not result.signal:
            notes.append("Nothing yet: favorite or star some repositories (F3 turns on history).")
        elif not result.items:
            notes.append("No suggestions found.")
        notes += [f"{h}: {m}" for h, m in result.errors.items()]
        self._status("  ".join(notes))

    def _saved_theme(self) -> str:
        try:
            return self.runner.settings.get_theme()
        except Exception:
            return "dark"

    def action_theme(self) -> None:
        """Switch between the dark and light theme; the choice is remembered."""
        light = self.theme != "textual-light"
        self.theme = "textual-light" if light else "textual-dark"
        try:
            self.runner.settings.set_theme("light" if light else "dark")
        except Exception:
            pass  # not remembered, still switched
        self.notify("Light theme" if light else "Dark theme", markup=False)

    def action_guided(self) -> None:
        if self.runner.enabled:
            self.runner.settings.set("guided_run", False)
            self.notify("Guided install is off", markup=False)
            return

        def done(ok: bool | None) -> None:
            if ok:
                self.runner.settings.set("guided_run", True)
                self.notify("Guided install is on. Every command still needs your approval.", markup=False)

        self.push_screen(ConfirmWrite("Turn on guided install?\nRepoHub will propose standard build commands for cloned "
                                      "repositories and run one only after you approve it.\n" + WARNING), done)

    def action_releases(self) -> None:
        self._status("Checking your favorites for new releases…")
        self.run_worker(self._show_releases(), exclusive=True)

    async def _show_releases(self) -> None:
        try:
            errors = await self.hub.check_releases()
        except Exception as e:
            self.notify(f"Could not check releases: {e}", severity="error", markup=False)
            return
        self.view = "releases"
        self.refresh_bindings()
        t = self._table()
        t.clear(columns=True)
        t.add_columns("Repo", "Host", "Release", "Published")
        for r, tag, published in self.hub.favorites.new_releases():
            t.add_row(Text(r.slug), Text(r.host), Text(tag), Text(published[:10]), key=f"repo:{r.host}:{r.slug}")
        notes = ["New releases in your favorites. m marks one as seen; Esc goes back."]
        notes += [f"{h}: {m}" for h, m in errors.items()]
        self._status("  ".join(notes))

    def action_mark_seen(self) -> None:
        if self.view != "releases":
            return
        key = self._folder_key()  # the highlighted row's key
        if key.startswith("repo:"):
            _, host, slug = key.split(":", 2)
            self.hub.favorites.mark_release_seen(f"{host}:{slug.lower()}")
            self.notify("Marked as seen", markup=False)
            self.run_worker(self._show_releases(), exclusive=True)

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

    # ---- folders: which folders RepoHub looks in for clones (scan only when asked)

    def action_folders(self) -> None:
        self._show_folders()

    def _show_folders(self, note: str = "") -> None:
        self.view = "folders"
        self.refresh_bindings()
        t = self._table()
        t.clear(columns=True)
        t.add_columns("Folder", "Repos", "Status")
        t.add_row(Text(str(self.clone_root)), Text("-"), Text("in use (clone folder)"), key="root:default")
        used = {str(self.clone_root)}
        for r in self.roots.list():
            used.add(r)
            t.add_row(Text(r), Text("-"), Text("in use (picked)"), key=f"root:{r}")
        for i, c in enumerate(self.scan_report.candidates if self.scan_report else []):
            if c.path not in used:
                t.add_row(Text(c.path), Text(str(c.repos)), Text("found: Enter to use"), key=f"cand:{i}")
        hint = "Folders. s scans your home folder, w the whole filesystem, Enter uses a found folder, d removes a picked one."
        if self.scan_report:
            r = self.scan_report
            hint += f"  Last scan: {r.dirs_seen} folders in {r.seconds}s{' (stopped early)' if r.truncated else ''}."
        self._status((note + "  " if note else "") + hint)

    def action_scan_home(self) -> None:
        self._start_scan(home_start())

    def action_scan_system(self) -> None:
        from pathlib import Path

        def done(ok: bool | None) -> None:
            if ok:
                self._start_scan(Path("/"))

        self.push_screen(ConfirmWrite("Scan the whole filesystem for git clones?\nSystem folders are skipped; it stops after 30 seconds."), done)

    def _start_scan(self, start) -> None:
        if self._scanning:
            self.notify("A scan is already running", markup=False)
            return
        self._scanning = True
        self._status("Scanning… (up to 30 seconds)")
        self.run_worker(self._scan(start), exclusive=False, group="scan")

    async def _scan(self, start) -> None:
        try:
            self.scan_report = await asyncio.wait_for(asyncio.to_thread(discover, start), BUDGET_SECONDS + 15)
        except (asyncio.TimeoutError, TimeoutError):
            self.notify("The scan took too long and was abandoned", severity="warning", markup=False)
            return
        except Exception as e:
            self.notify(f"Scan failed: {e}", severity="error", markup=False)
            return
        finally:
            self._scanning = False
        if self.view == "folders":
            self._show_folders(f"Found {len(self.scan_report.candidates)} folder(s) with clones.")

    def _folder_key(self) -> str:
        t = self._table()
        if t.row_count == 0:
            return ""
        try:
            return t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value or ""
        except Exception:
            return ""

    def _use_folder(self, index: int) -> None:
        cands = self.scan_report.candidates if self.scan_report else []
        if not 0 <= index < len(cands):
            return
        path = cands[index].path

        def done(ok: bool | None) -> None:
            if ok:
                added = self.roots.add([path])
                self.awareness.invalidate()
                self._show_folders("Added." if added else "Could not add that folder.")

        self.push_screen(ConfirmWrite(f"Look for clones in this folder too?\n{path}"), done)

    def _drop_folder(self, path: str) -> None:
        def done(ok: bool | None) -> None:
            if ok:
                self.roots.remove(path)
                self.awareness.invalidate()
                self._show_folders("Removed.")

        self.push_screen(ConfirmWrite(f"Stop looking for clones in this folder?\n{path}"), done)

    def action_remove_favorite(self) -> None:
        """Favorites view: remove the selected row without opening it. Folders view: stop using a picked folder."""
        if self.view == "folders":
            key = self._folder_key()
            if key.startswith("root:") and key != "root:default":
                self._drop_folder(key[5:])
            return
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
            t.add_row(Text(self._mark(r) + r.slug), Text(r.host), Text(str(r.stars)), Text(r.language or "n/a"),
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
            t.add_row(Text(self._mark(r) + r.slug), Text(r.host), Text(str(r.stars)), Text(r.language or "n/a"),
                      Text((item.note or r.description)[:70]), key=key)
        first = page.offset + 1 if page.items else 0
        notes = [f"{shelf.name}: {first}-{page.offset + len(page.items)} of {page.total}  (as of {shelf.as_of or 'n/a'})"]
        notes += [f"{h}: {m}" for h, m in page.errors.items()]
        self._status("  ".join(notes))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        key = event.row_key.value or ""
        if key.startswith("cand:") and self.view == "folders":
            self._use_folder(int(key[5:]))
        elif key == "recommended":
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
