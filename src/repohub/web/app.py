from __future__ import annotations

import argparse
import asyncio
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


def create_app(hub, clone_root, session_token: str | None = None, shelves=None, cloner=do_clone,
               allowed_hosts=("127.0.0.1", "localhost")) -> FastAPI:
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
        resp = await call_next(request)
        resp.headers["Content-Security-Policy"] = CSP
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp

    def page(request: Request, name: str, status: int = 200, **ctx):
        host_ids = ["all", *registry().ids]
        return templates.TemplateResponse(request, name, {"token": token, "form": {}, "q": "", "host_ids": host_ids, **ctx},
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
                    is_fav=hub.favorites.is_favorite(d.repo.key), signed_in=acct.status == "signed in", starred=starred)

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
    async def favorites_page(request: Request):
        if not refresh_tasks:  # one background refresh at a time; the page never waits for the network
            task = asyncio.create_task(hub.refresh_favorites())
            refresh_tasks.add(task)
            task.add_done_callback(_refresh_done(refresh_tasks))
        return page(request, "favorites.html", repos=hub.favorites.list(), registered=registry().ids)

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


def main(argv: list[str] | None = None) -> None:
    import uvicorn

    from repohub.config import build_hub, clone_root

    parser = argparse.ArgumentParser(prog="repohub-web", description="RepoHub web app (binds to loopback by default)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    if args.host not in ("127.0.0.1", "localhost"):
        print("WARNING: binding to a non-loopback address exposes clone and favorites to your network.")
    uvicorn.run(create_app(build_hub(), clone_root()), host=args.host, port=args.port)
