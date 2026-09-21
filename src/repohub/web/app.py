from __future__ import annotations

import argparse
import asyncio
import shlex
import threading
from contextlib import asynccontextmanager
import secrets
from pathlib import Path

import nh3
from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from repohub.core.awareness import Awareness
from repohub.core.external import GHGRAB_INSTALL, find_tool, grab_command, release_command
from repohub.core.runplan import WARNING
from repohub.core.runner import RunError, Runner
from repohub.core.settings import Settings
from repohub.core.roots import BUDGET_SECONDS, ScanRoots, discover, home_start
from repohub.core.browse import load_all_shelves
from repohub.core.clone import CloneError, clone as do_clone, clone_url, plan_clone
from repohub.core.hosts import registry
from repohub.core.models import SORTS, SearchFilters
from repohub.core.hub import CURATED_PAGE
from repohub.core.providers.base import ActionDenied, Conflict, NotFound, ProviderError, RateLimited
from repohub.core.models import MAX_DAYS, MAX_STARS
from repohub.core.queryparse import parse_query

HERE = Path(__file__).parent
CSP = ("default-src 'self'; img-src * data:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
       "frame-ancestors 'none'; form-action 'self'; base-uri 'none'")
MAX_BANNERS = 10
_md = MarkdownIt("commonmark", {"html": False})


def render_markdown(text: str) -> str:
    return nh3.clean(_md.render(text or ""), link_rel="noopener noreferrer")


_FALSE = {"", "0", "false", "off", "no"}


def _checked(raw: str) -> bool:
    """Checkbox value: empty, 0, false, off and no (any case) are off; anything else is on."""
    return (raw or "").strip().lower() not in _FALSE


def _int_param(raw: str, name: str, maximum: int) -> int:
    raw = (raw or "").strip()
    if not raw:
        return 0
    try:
        value = int(raw)
    except ValueError:
        raise HTTPException(422, f"{name} must be a whole number") from None
    if not 0 <= value <= maximum:
        raise HTTPException(422, f"{name} must be between 0 and {maximum}")
    return value


def _refresh_done(tasks: set):
    def done(task: asyncio.Task) -> None:
        tasks.discard(task)
        if not task.cancelled():
            task.exception()  # mark as retrieved; a failed background refresh is not fatal

    return done


SCAN_TIMEOUT = BUDGET_SECONDS + 15  # hard limit around a scan that a slow disk or mount keeps blocked


def create_app(hub, clone_root, session_token: str | None = None, shelves=None, cloner=do_clone,
               allowed_hosts=("127.0.0.1", "localhost"), awareness: Awareness | None = None,
               roots: ScanRoots | None = None, runner: Runner | None = None) -> FastAPI:
    aware = awareness if awareness is not None else Awareness(clone_root)
    scan_roots = roots if roots is not None else ScanRoots()
    guided = runner if runner is not None else Runner(aware, Settings(), hub.actions)
    folder_state: dict = {"report": None, "running": False}  # the last scan lives in memory only
    token = session_token or secrets.token_urlsafe(32)
    if shelves is None:
        loaded = load_all_shelves()
        shelf_list, problems = list(loaded.shelves), list(loaded.problems)
    else:
        shelf_list, problems = shelves, []
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    refresh_tasks: set[asyncio.Task] = set()

    @asynccontextmanager
    async def lifespan(_app):
        yield
        for t in list(refresh_tasks):
            t.cancel()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.refresh_tasks = refresh_tasks
    app.state.shelf_problems = problems
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(allowed_hosts))
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        await run_in_threadpool(aware.cloned)  # refresh the clone scan off the event loop; pages then read the cache
        resp = await call_next(request)
        resp.headers["Content-Security-Policy"] = CSP
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp

    def page(request: Request, name: str, status: int = 200, **ctx):
        host_ids = ["all", *registry().ids]
        return templates.TemplateResponse(request, name, {"token": token, "form": {}, "q": "", "host_ids": host_ids, "cloned": aware.cloned(), **ctx},
                                          status_code=status)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        return page(request, "_message.html", status=exc.status_code, message=str(exc.detail))

    def require_token(value: str) -> None:
        if not secrets.compare_digest((value or "").encode("utf-8"), token.encode("utf-8")):
            raise HTTPException(403, "missing or invalid session token")

    def require_repo(host: str, slug: str) -> None:
        if not registry().slug_ok(host, slug):
            raise HTTPException(404, "not found")

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request):
        return page(request, "home.html", shelves=list(enumerate(shelf_list)), problems=[*hub.host_problems, *problems][:MAX_BANNERS])

    @app.get("/shelf/{index}", response_class=HTMLResponse)
    async def shelf(request: Request, index: int):
        if not 0 <= index < len(shelf_list):
            raise HTTPException(404, "no such shelf")
        s = shelf_list[index]
        if s.curated:  # snapshots only: the home page must not spend API rate limit
            return page(request, "_curated.html", page=await hub.curated_page(s, 0, 6, refresh=False))
        return page(request, "_results.html", result=await hub.shelf(s), limit=6)

    @app.get("/shelves/{index}", response_class=HTMLResponse)
    async def shelf_page(request: Request, index: int, page_no: str = Query("1", alias="page")):
        if not 0 <= index < len(shelf_list):
            raise HTTPException(404, "no such shelf")
        s = shelf_list[index]
        pages = max(1, -(-len(s.repos) // CURATED_PAGE)) if s.curated else 1
        raw = (page_no or "").strip() or "1"
        if not raw.isascii() or not raw.isdigit() or not 1 <= int(raw) <= pages:
            raise HTTPException(404, "no such page")
        n = int(raw)
        if s.curated:
            cp = await hub.curated_page(s, (n - 1) * CURATED_PAGE, CURATED_PAGE)
            first, last = cp.offset + 1, cp.offset + len(cp.items)
            return page(request, "shelf.html", index=index, shelf=s, page=cp, page_no=n, pages=pages,
                        first=first, last=last)
        return page(request, "shelf.html", index=index, shelf=s, result=await hub.shelf(s), limit=None,
                    page_no=1, pages=1)

    @app.get("/search", response_class=HTMLResponse)
    async def search(request: Request, q: str = "", language: str = "", min_stars: str = "",
                     days: str = "",
                     host: str = "all", archived: str = "", sort: str = "stars", hide_forks: str = ""):
        if host not in ("all", "both") and host not in registry().ids:
            raise HTTPException(400, "bad host")
        if sort not in SORTS:
            raise HTTPException(400, "bad sort")
        min_stars = _int_param(min_stars, "min_stars", MAX_STARS)
        days = _int_param(days, "days", MAX_DAYS)
        filters = SearchFilters(language=language or None, min_stars=min_stars,
                                updated_within_days=days or None, hosts=registry().ids if host in ("all", "both") else (host,),
                                include_archived=_checked(archived), sort=sort, hide_forks=_checked(hide_forks))
        parsed = parse_query(q, filters)
        result = await hub.search(parsed.text, parsed.filters)
        return page(request, "search.html", q=q, result=result, limit=None, problems=parsed.problems[:11],
                    form=dict(language=language, min_stars=min_stars, days=days, host=host, archived=_checked(archived),
                              sort=sort, hide_forks=_checked(hide_forks)))

    @app.get("/repo/{host}/{slug:path}", response_class=HTMLResponse)
    async def repo_page(request: Request, host: str, slug: str):
        require_repo(host, slug)
        try:
            d = await hub.detail(host, slug)
        except NotFound as e:
            return page(request, "_message.html", status=404, message=f"Could not load {slug}: {e}")
        except ProviderError as e:
            return page(request, "_message.html", status=502, message=f"Could not load {slug}: {e}")
        hub.record_view(d.repo)  # only stored while the opt-in history is on
        acct = await hub.account(host)
        starred = await hub.starred(host, slug) if acct.status == "signed in" else None
        return page(request, "repo.html", d=d, readme_html=render_markdown(d.readme) if d.readme else "",
                    is_fav=hub.favorites.is_favorite(d.repo.key), signed_in=acct.status == "signed in", starred=starred,
                    verdict=await run_in_threadpool(aware.check, d.repo, d.release),
                    claude_cmd=(f"cd {shlex.quote(c.path)} && claude" if (c := aware.clone_of(d.repo)) else ""),
                    grab_cmd=grab_command(host, d.repo.slug), grab_rel_cmd=release_command(host, d.repo.slug),
                    grab_installed=find_tool("ghgrab") is not None, grab_install=GHGRAB_INSTALL)

    def _in_use() -> list[str]:
        return [str(r) for r in aware.roots()]

    @app.get("/folders", response_class=HTMLResponse)
    async def folders_page(request: Request):
        return page(request, "folders.html", default=str(clone_root), roots=await run_in_threadpool(scan_roots.list),
                    report=folder_state["report"], running=folder_state["running"])

    @app.post("/folders/scan")
    async def folders_scan(scope: str = Form(...), token_field: str = Form("", alias="token")):
        """Look for clones below the home folder (or the whole filesystem) - only when the user asks."""
        require_token(token_field)
        if scope not in ("home", "system"):
            raise HTTPException(404, "not found")
        if folder_state["running"]:
            raise HTTPException(429, "a scan is already running")
        folder_state["running"] = True
        try:
            start = home_start() if scope == "home" else Path("/")
            folder_state["report"] = await asyncio.wait_for(run_in_threadpool(discover, start), SCAN_TIMEOUT)
        except (asyncio.TimeoutError, TimeoutError):
            raise HTTPException(504, "the scan took too long and was abandoned; try a narrower place") from None
        finally:
            folder_state["running"] = False
        return RedirectResponse("/folders", status_code=303)

    @app.post("/folders/add")
    async def folders_add(path: list[str] = Form(default=[]), token_field: str = Form("", alias="token")):
        require_token(token_field)
        report = folder_state["report"]
        allowed = {c.path for c in report.candidates} if report else set()
        picked = [p for p in path[:50] if p in allowed]  # only folders the last scan actually found
        if picked:
            await run_in_threadpool(scan_roots.add, picked)
            aware.invalidate()
        return RedirectResponse("/folders", status_code=303)

    @app.post("/folders/remove")
    async def folders_remove(path: str = Form(...), token_field: str = Form("", alias="token")):
        require_token(token_field)
        if not await run_in_threadpool(scan_roots.remove, path):
            raise HTTPException(404, "not found")
        aware.invalidate()
        return RedirectResponse("/folders", status_code=303)

    # ---- guided install and run: opt-in, one approved command at a time (see docs/plans/2026-09-22-phase7-design.md)

    @app.get("/run/{host}/{slug:path}", response_class=HTMLResponse)
    async def run_plan_page(request: Request, host: str, slug: str):
        require_repo(host, slug)
        clone, plan = await run_in_threadpool(guided.find, host, slug)
        return page(request, "run_plan.html", host=host, slug=slug, clone=clone, plan=plan, enabled=guided.enabled,
                    current=guided.current(), warning=WARNING)

    @app.post("/runsetting")
    async def run_setting(host: str = Form(...), slug: str = Form(...), action: str = Form(...),
                          token_field: str = Form("", alias="token")):
        require_token(token_field)
        require_repo(host, slug)
        if action not in ("on", "off"):
            raise HTTPException(404, "not found")
        guided.settings.set("guided_run", action == "on")
        return RedirectResponse(f"/run/{host}/{slug}", status_code=303)

    @app.post("/runstart")
    async def run_start(request: Request, host: str = Form(...), slug: str = Form(...), step: str = Form(...),
                        digest: str = Form(...), token_field: str = Form("", alias="token")):
        require_token(token_field)
        require_repo(host, slug)
        try:
            session = await run_in_threadpool(guided.start, host, slug, step, digest)
        except RunError as e:
            return page(request, "_message.html", status=409, message=str(e))
        return RedirectResponse(f"/runs/{session.id}", status_code=303)

    def find_session(session_id: str):
        session = guided.get(session_id)
        if session is None:
            raise HTTPException(404, "not found")
        return session

    @app.get("/runs/{session_id}", response_class=HTMLResponse)
    async def run_session_page(request: Request, session_id: str):
        return page(request, "run_session.html", s=find_session(session_id), warning=WARNING)

    @app.get("/runs/{session_id}/output", response_class=HTMLResponse)
    async def run_output(request: Request, session_id: str):
        return page(request, "_run_output.html", s=find_session(session_id))

    @app.post("/runs/{session_id}/cancel")
    async def run_cancel(session_id: str, token_field: str = Form("", alias="token")):
        require_token(token_field)
        find_session(session_id)
        guided.cancel(session_id)
        return RedirectResponse(f"/runs/{session_id}", status_code=303)

    @app.get("/recommended", response_class=HTMLResponse)
    async def recommended(request: Request):
        result = await hub.recommend(6)
        if not result.items:  # no signal (or nothing found): the home page shows nothing
            return HTMLResponse("")
        return page(request, "_recs.html", result=result, title="Recommended for you")

    @app.get("/similar/{host}/{slug:path}", response_class=HTMLResponse)
    async def similar(request: Request, host: str, slug: str):
        require_repo(host, slug)
        try:
            result = await hub.similar(host, slug, 6)
        except ProviderError:
            return HTMLResponse("")
        if not result.items:
            return HTMLResponse("")
        return page(request, "_recs.html", result=result, title="Similar repositories")

    @app.post("/history")
    async def history_control(action: str = Form(...), token_field: str = Form("", alias="token")):
        require_token(token_field)
        if action == "on":
            hub.history.set_enabled(True)
        elif action == "off":
            hub.history.set_enabled(False)
        elif action == "clear":
            hub.history.clear()
        else:
            raise HTTPException(404, "not found")
        return RedirectResponse("/accounts", status_code=303)

    @app.get("/accounts", response_class=HTMLResponse)
    async def accounts_page(request: Request):
        return page(request, "accounts.html", accounts=await hub.accounts(), recent=hub.recent_actions(20),
                    history_on=hub.history.enabled, history_count=len(hub.history.list()))

    @app.get("/favorites", response_class=HTMLResponse)
    async def favorites_page(request: Request, tag: str = "", collection: str = "", q: str = ""):
        if not refresh_tasks:  # one background refresh at a time; the page never waits for the network
            task = asyncio.create_task(hub.refresh_favorites())
            refresh_tasks.add(task)
            task.add_done_callback(_refresh_done(refresh_tasks))
        favs = hub.favorites
        tag = favs.clean_tag(tag)
        return page(request, "favorites.html", repos=favs.list(tag=tag or None, collection=collection[:40] or None, q=q[:100]),
                    registered=registry().ids, notes=favs.notes(), tags=favs.tags_by_key(), memberships=favs.collections_by_key(),
                    all_tags=favs.all_tags(), collections=favs.collections(), f_tag=tag, f_collection=collection[:40], f_q=q[:100],
                    new_count=len(favs.new_releases()))

    def fav_key(host: str, slug: str) -> str:
        if len(slug) > 200 or not hub.favorites.is_favorite(f"{host}:{slug.lower()}"):
            raise HTTPException(404, "not found")
        return f"{host}:{slug.lower()}"

    @app.post("/favorites/note")
    async def favorite_note(host: str = Form(...), slug: str = Form(...), note: str = Form(""),
                            token_field: str = Form("", alias="token")):
        require_token(token_field)
        hub.favorites.set_note(fav_key(host, slug), note)
        return RedirectResponse("/favorites", status_code=303)

    @app.post("/favorites/tag")
    async def favorite_tag(host: str = Form(...), slug: str = Form(...), tag: str = Form(...), action: str = Form("add"),
                           token_field: str = Form("", alias="token")):
        require_token(token_field)
        key = fav_key(host, slug)
        if action == "add":
            hub.favorites.add_tag(key, tag)
        elif action == "remove":
            hub.favorites.remove_tag(key, tag)
        else:
            raise HTTPException(404, "not found")
        return RedirectResponse("/favorites", status_code=303)

    @app.post("/favorites/collection")
    async def favorite_collection(action: str = Form(...), name: str = Form(""), host: str = Form(""), slug: str = Form(""),
                                  token_field: str = Form("", alias="token")):
        require_token(token_field)
        favs = hub.favorites
        if action == "create":
            favs.create_collection(name)
        elif action == "delete":
            favs.delete_collection(name[:40])
        elif action in ("add", "remove"):
            key = fav_key(host, slug)
            (favs.add_to_collection if action == "add" else favs.remove_from_collection)(name[:40], key)
        else:
            raise HTTPException(404, "not found")
        return RedirectResponse("/favorites", status_code=303)

    @app.get("/favorites/releases", response_class=HTMLResponse)
    async def favorites_releases(request: Request):
        errors = await hub.check_releases()
        return page(request, "releases.html", items=hub.favorites.new_releases(), errors=errors)

    @app.post("/favorites/seen")
    async def favorites_seen(host: str = Form(""), slug: str = Form(""), tag: str = Form(""), token_field: str = Form("", alias="token")):
        require_token(token_field)
        if host or slug:
            hub.favorites.mark_release_seen(fav_key(host, slug), tag or None)
        else:
            hub.favorites.mark_release_seen()
        return RedirectResponse("/favorites/releases", status_code=303)

    @app.post("/favorite", response_class=HTMLResponse)
    async def toggle_favorite(request: Request, host: str = Form(...), slug: str = Form(...), token_field: str = Form("", alias="token")):
        require_token(token_field)
        if host not in registry():
            # A host that is no longer configured: a stored favorite can be removed (no network),
            # but nothing is ever added for it.
            key = f"{host}:{slug.lower()}"
            if len(slug) <= 200 and hub.favorites.is_favorite(key):
                hub.favorites.remove(key)
                return HTMLResponse('<div class="card"><p class="dim">Removed from favorites.</p></div>')
            raise HTTPException(404, "not found")
        require_repo(host, slug)
        try:
            repo = (await hub.detail(host, slug)).repo
            key = repo.key
        except NotFound as e:
            return page(request, "_message.html", status=404, message=str(e))
        except ProviderError as e:
            # Offline: still allow removing a favorite stored under the posted key.
            key = f"{host}:{slug.lower()}"
            if not hub.favorites.is_favorite(key):
                return page(request, "_message.html", status=502, message=str(e))
            repo = None
        if hub.favorites.is_favorite(key):
            hub.favorites.remove(key)
            is_fav = False
        else:
            hub.favorites.add(repo)
            is_fav = True
        return page(request, "_fav_button.html", host=host, slug=slug, is_fav=is_fav)

    @app.get("/clone", response_class=HTMLResponse)
    async def clone_confirm(request: Request, host: str, slug: str):
        require_repo(host, slug)
        try:
            target = plan_clone(clone_url(host, slug), clone_root)
        except CloneError as e:
            return page(request, "_message.html", message=str(e))
        return page(request, "clone_confirm.html", host=host, slug=slug, target=target)

    @app.post("/clone", response_class=HTMLResponse)
    async def clone_run(request: Request, host: str = Form(...), slug: str = Form(...), token_field: str = Form("", alias="token")):
        require_token(token_field)
        require_repo(host, slug)
        try:
            target = await run_in_threadpool(cloner, clone_url(host, slug), clone_root)
        except CloneError as e:
            return page(request, "_message.html", message=f"Clone failed: {e}")
        return page(request, "_message.html", message=f"Cloned to {target}")

    async def confirm_write(request: Request, host: str, slug: str, action: str):
        """A confirmation page only: it changes nothing. It names the host, repository and account."""
        require_repo(host, slug)
        acct = await hub.account(host)
        if acct.status != "signed in":
            return page(request, "_message.html", status=403,
                        message=f"Not signed in on {host}. Set a token for it and check the Accounts page.")
        return page(request, "write_confirm.html", host=host, slug=slug, action=action, acct=acct)

    async def run_write(request: Request, host: str, slug: str, action: str, token_field: str):
        require_token(token_field)
        require_repo(host, slug)
        try:
            if action == "fork":
                res = await hub.fork(host, slug)
                return page(request, "fork_done.html", host=host, slug=slug, fork=res)
            await hub.set_star(host, slug, action == "star")
        except ActionDenied as e:
            return page(request, "_message.html", status=403, message=str(e))
        except RateLimited as e:
            return page(request, "_message.html", status=429, message=str(e))
        except NotFound as e:
            return page(request, "_message.html", status=404, message=str(e))
        except Conflict as e:
            return page(request, "_message.html", status=409, message=str(e))
        except ProviderError as e:
            return page(request, "_message.html", status=502, message=f"{action} failed: {e}")
        return page(request, "_message.html", message=("Starred " if action == "star" else "Unstarred ") + slug)

    @app.get("/star", response_class=HTMLResponse)
    async def star_confirm(request: Request, host: str, slug: str, action: str = "star"):
        if action not in ("star", "unstar"):
            raise HTTPException(404, "not found")
        return await confirm_write(request, host, slug, action)

    @app.post("/star", response_class=HTMLResponse)
    async def star_run(request: Request, host: str = Form(...), slug: str = Form(...), action: str = Form(...),
                       token_field: str = Form("", alias="token")):
        if action not in ("star", "unstar"):
            raise HTTPException(404, "not found")
        return await run_write(request, host, slug, action, token_field)

    @app.get("/fork", response_class=HTMLResponse)
    async def fork_confirm(request: Request, host: str, slug: str):
        return await confirm_write(request, host, slug, "fork")

    @app.post("/fork", response_class=HTMLResponse)
    async def fork_run(request: Request, host: str = Form(...), slug: str = Form(...),
                       token_field: str = Form("", alias="token")):
        return await run_write(request, host, slug, "fork", token_field)

    return app


def browse_url(host: str, port: int) -> str | None:
    """Where a browser on this machine can reach the app, or None when the bind address is not reachable that way.

    The app only answers requests addressed to 127.0.0.1 or localhost (a Host-header check), so a specific LAN or IPv6
    address gets no automatic browser.
    """
    if host in ("127.0.0.1", "localhost", "0.0.0.0"):
        return f"http://{'127.0.0.1' if host == '0.0.0.0' else host}:{port}/"
    return None


def open_when_ready(url: str, host: str, port: int, opener=None, tries: int = 60, fallback=None) -> threading.Thread:
    """Open ``url`` in the default browser as soon as the server accepts connections (gives up after ~15 s).

    If Python's ``webbrowser`` finds no browser (for example inside a sandbox with no display socket), ``xdg-open`` is tried.
    """
    import shutil
    import socket
    import subprocess
    import time
    import webbrowser

    opener = opener or webbrowser.open

    def xdg(u: str) -> bool:
        exe = shutil.which("xdg-open")
        return bool(exe) and subprocess.run([exe, u], timeout=20, capture_output=True).returncode == 0

    fallback = fallback or xdg

    def wait_and_open() -> None:
        probe = "127.0.0.1" if host in ("0.0.0.0", "::", "localhost") else host
        for _ in range(tries):
            try:
                with socket.create_connection((probe, port), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.25)
        else:
            return
        try:
            if opener(url) is False:
                fallback(url)
        except Exception:
            pass  # no browser available: the URL is still printed by the server

    t = threading.Thread(target=wait_and_open, name="repohub-open", daemon=True)
    t.start()
    return t


def main(argv: list[str] | None = None) -> None:
    import uvicorn

    from repohub.config import build_hub, clone_root, make_settings

    parser = argparse.ArgumentParser(prog="repohub-web", description="RepoHub web app (binds to loopback by default)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", help="open the web app in your browser once it is running")
    args = parser.parse_args(argv)
    if args.host not in ("127.0.0.1", "localhost"):
        print("WARNING: binding to a non-loopback address exposes clone and favorites to your network.")
    roots = ScanRoots()
    hub = build_hub()
    aware = Awareness(clone_root(), extra_roots=roots.list)
    app = create_app(hub, clone_root(), roots=roots, awareness=aware, runner=Runner(aware, make_settings(), hub.actions))
    if args.open:
        url = browse_url(args.host, args.port)
        if url:
            open_when_ready(url, args.host, args.port)
        else:
            print(f"--open skipped: browse to http://127.0.0.1:{args.port}/ ({args.host} is not reachable that way)")
    uvicorn.run(app, host=args.host, port=args.port)
