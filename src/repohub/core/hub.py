from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import asdict, dataclass
from typing import Callable

from repohub.core.accounts import (ANONYMOUS, ERROR, LIMITED, REJECTED, SIGNED_IN, UNAVAILABLE, AccountInfo,
                                    star_fork_hint)
from repohub.core.actionlog import ActionEntry, ActionLog
from repohub.core.browse import Shelf, ShelfEntry
from repohub.core.cache import Cache
from repohub.core.hosts import registry
from repohub.core.models import Release, Repo, SearchFilters
from repohub.core.providers.base import ActionDenied, ForkResult, NotFound, ProviderError, RateLimited
from repohub.core.search import SearchResult, search_all, with_deadline
from repohub.core.store import Favorites

SEARCH_TTL = 600
DETAIL_TTL = 3600
DEGRADED_TTL = 60
PARTIAL_TTL = 60
ACCOUNT_TTL = 300
STARRED_TTL = 60
MIN_LIMIT_WAIT = 60
MAX_LIMIT_WAIT = 3600
REFRESH_CONCURRENCY = 8
MAX_README_CHARS = 200_000
CURATED_PAGE = 12
MAX_CURATED_LIMIT = 50


@dataclass
class Detail:
    repo: Repo
    readme: str | None
    release: Release | None

    def to_dict(self) -> dict:
        return {"repo": self.repo.to_dict(), "readme": self.readme,
                "release": self.release.to_dict() if self.release else None}

    @classmethod
    def from_dict(cls, d: dict) -> "Detail":
        return cls(Repo.from_dict(d["repo"]), d["readme"], Release.from_dict(d["release"]) if d["release"] else None)


@dataclass
class CuratedItem:
    repo: Repo
    note: str
    as_of: str | None
    live: bool


@dataclass
class CuratedPage:
    items: list[CuratedItem]
    total: int
    offset: int
    limit: int
    errors: dict[str, str]


def repo_from_snapshot(entry: ShelfEntry, as_of: str | None) -> Repo:
    """Network-free Repo for a curated entry, built from its (optional) snapshot."""
    snap = entry.snapshot
    spec = registry().get(entry.host)  # an unconfigured host has no known domain: no link
    return Repo(host=entry.host, slug=entry.slug, url=f"{spec.web_base}/{entry.slug}" if spec else "",
                description=snap.description if snap else "", stars=snap.stars if snap else 0,
                language=snap.language if snap else "", license=snap.license if snap else "",
                topics=(), pushed_at=snap.pushed_at if snap else "", archived=False, forks=0,
                homepage="", fork=False)


NOT_CONFIGURED = "host not configured"


def _domain(host: str) -> str:
    """The host's configured domain, for cache keys (a repointed id must not reuse old entries)."""
    spec = registry().get(host)
    return spec.domain if spec else "?"


class Hub:
    def __init__(self, providers: dict, cache: Cache, favorites: Favorites, *,
                 clock: Callable[[], float] = time.time, host_problems: list[str] | None = None,
                 token_sources: dict[str, str] | None = None, actions: ActionLog | None = None):
        self.providers, self.cache, self.favorites = providers, cache, favorites
        self.host_problems: list[str] = list(host_problems) if host_problems else []
        self.token_sources: dict[str, str] = dict(token_sources or {})  # host -> "env NAME" / "gh CLI"
        self.actions = actions if actions is not None else ActionLog()
        self._clock = clock
        self._starred: dict[str, tuple[float, bool]] = {}  # in memory only
        self._accounts: dict[str, tuple[float, AccountInfo]] = {}  # in memory only, never on disk
        self._limited_until: dict[str, tuple[float, str]] = {}  # host -> (until, message)

    def _note_rate_limit(self, host: str, err: RateLimited) -> None:
        now = self._clock()
        wait = MIN_LIMIT_WAIT
        if isinstance(err.reset_at, (int, float)) and not isinstance(err.reset_at, bool):
            wait = max(MIN_LIMIT_WAIT, min(err.reset_at - now, MAX_LIMIT_WAIT))
        self._limited_until[host] = (now + wait, str(err))

    def _limit_message(self, host: str) -> str | None:
        hit = self._limited_until.get(host)
        if hit is None:
            return None
        if self._clock() < hit[0]:
            return hit[1]
        del self._limited_until[host]
        return None

    async def search(self, query: str, filters: SearchFilters | None = None) -> SearchResult:
        filters = filters or SearchFilters()
        searched = sorted((h, _domain(h)) for h in filters.hosts)
        digest = hashlib.sha256(json.dumps([query, asdict(filters), searched], sort_keys=True).encode()).hexdigest()
        key = f"search:{digest}"
        hit = self.cache.get(key)
        if hit is not None:
            return SearchResult.from_dict(hit)
        result = await search_all(self.providers, query, filters)
        if not result.errors:
            self.cache.set(key, result.to_dict(), SEARCH_TTL)
            return result
        if not result.repos:
            stale = self.cache.get(key, allow_stale=True)
            if stale is not None:
                old = SearchResult.from_dict(stale)
                return SearchResult(old.repos, result.errors, stale=True)
        asked = [h for h in filters.hosts if h in self.providers]
        if len(result.errors) < len(asked):  # some hosts answered: keep the partial result briefly
            self.cache.set(key, result.to_dict(), PARTIAL_TTL)
        return result

    async def shelf(self, shelf: Shelf) -> SearchResult:
        if shelf.curated:
            page = await self.curated_page(shelf, 0, CURATED_PAGE)
            return SearchResult([item.repo for item in page.items], page.errors)
        return await self.search(shelf.query, shelf.filters())

    async def repo_summary(self, host: str, slug: str) -> Repo:
        provider = self.providers.get(host)
        if provider is None:
            raise ProviderError(host, NOT_CONFIGURED)
        key = f"repo:{host}@{_domain(host)}:{slug.lower()}"
        hit = self.cache.get(key)
        if hit is not None:
            return Repo.from_dict(hit)
        repo = await with_deadline(host, provider.repo(slug))
        self.cache.set(key, repo.to_dict(), DETAIL_TTL)
        return repo

    async def curated_page(self, shelf: Shelf, offset: int = 0, limit: int = CURATED_PAGE, *,
                           refresh: bool = True) -> CuratedPage:
        offset = max(0, offset)
        limit = min(max(1, limit), MAX_CURATED_LIMIT)
        entries = shelf.repos[offset:offset + limit]
        if not refresh:  # snapshot-only: no provider call, no errors (protects the API rate limit)
            return CuratedPage([CuratedItem(repo_from_snapshot(e, shelf.as_of), e.note, shelf.as_of, False)
                                for e in entries], len(shelf.repos), offset, limit, {})
        sem = asyncio.Semaphore(REFRESH_CONCURRENCY)

        async def one(entry: ShelfEntry) -> tuple[Repo | None, str | None]:
            try:
                async with sem:
                    if entry.host not in self.providers:  # never call a provider for an unconfigured host
                        return None, NOT_CONFIGURED
                    limited = self._limit_message(entry.host)
                    if limited is not None:  # host rate-limited: keep the snapshot, no request
                        return None, limited
                    return await self.repo_summary(entry.host, entry.slug), None
            except RateLimited as e:
                self._note_rate_limit(entry.host, e)
                return None, str(e)
            except ProviderError as e:
                return None, str(e)
            except Exception:
                return None, "unexpected error"

        tasks = [asyncio.ensure_future(one(e)) for e in entries]
        try:
            results = await asyncio.gather(*tasks)
        finally:
            pending = [t for t in tasks if not t.done()]
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        items: list[CuratedItem] = []
        errors: dict[str, str] = {}
        for entry, (fresh, err) in zip(entries, results):  # entry order, not returned slug
            if fresh is None:
                errors.setdefault(entry.host, err or "unexpected error")
            items.append(CuratedItem(fresh or repo_from_snapshot(entry, shelf.as_of), entry.note,
                                     shelf.as_of, fresh is not None))
        return CuratedPage(items, len(shelf.repos), offset, limit, errors)

    async def detail(self, host: str, slug: str) -> Detail:
        provider = self.providers.get(host)
        if provider is None:
            raise ProviderError(host, NOT_CONFIGURED)
        key = f"detail:{host}@{_domain(host)}:{slug.lower()}"
        hit = self.cache.get(key)
        if hit is not None:
            return Detail.from_dict(hit)
        try:
            repo, readme, release = await asyncio.gather(
                with_deadline(host, provider.repo(slug)), with_deadline(host, provider.readme(slug)),
                with_deadline(host, provider.latest_release(slug)), return_exceptions=True)
            if isinstance(repo, BaseException):
                raise repo
        except NotFound:
            raise
        except ProviderError:
            stale = self.cache.get(key, allow_stale=True)
            if stale is not None:
                return Detail.from_dict(stale)
            raise
        degraded = False
        for part in (readme, release):
            if isinstance(part, BaseException) and not isinstance(part, Exception):
                raise part
        for part in (readme, release):
            if isinstance(part, ProviderError):
                degraded = True
            elif isinstance(part, Exception):
                raise part
        readme = None if isinstance(readme, BaseException) else readme
        release = None if isinstance(release, BaseException) else release
        if readme is not None and len(readme) > MAX_README_CHARS:
            readme = readme[:MAX_README_CHARS] + "\n\n_[README truncated]_"
        detail = Detail(repo, readme, release)
        self.cache.set(key, detail.to_dict(), DEGRADED_TTL if degraded else DETAIL_TTL)
        return detail

    async def refresh_favorites(self, max_age: float = 86400) -> None:
        sem = asyncio.Semaphore(REFRESH_CONCURRENCY)

        async def one(repo: Repo) -> None:
            provider = self.providers.get(repo.host)
            if provider is None:
                return
            try:
                async with sem:
                    fresh = await with_deadline(repo.host, provider.repo(repo.slug))
                self.favorites.update(fresh)
            except ProviderError:
                self.favorites.mark_checked(repo.key)  # retry at most once per max_age

        await asyncio.gather(*(one(r) for r in self.favorites.stale(max_age)))

    async def account(self, host: str, *, refresh: bool = False) -> AccountInfo:
        """Who RepoHub is signed in as on one host. Cached in memory for a few minutes; read-only."""
        spec = registry().get(host)
        provider = self.providers.get(host)
        if spec is None or provider is None:
            return AccountInfo(host, spec.name if spec else host, UNAVAILABLE)
        key = f"{host}@{spec.domain}"
        hit = self._accounts.get(key)
        if getattr(provider, "token_rejected", False):  # a token dropped since caching must show at once
            self._accounts.pop(key, None)
        elif hit is not None and not refresh and self._clock() < hit[0]:
            return hit[1]
        info = await self._fetch_account(spec, provider)
        if info.status in (SIGNED_IN, ANONYMOUS, REJECTED):  # errors and limits are retried next time
            self._accounts[key] = (self._clock() + ACCOUNT_TTL, info)
        return info

    async def _fetch_account(self, spec, provider) -> AccountInfo:
        source = self.token_sources.get(spec.id, "")
        base = {"host": spec.id, "name": spec.name, "source": source}
        if getattr(provider, "token_rejected", False):
            return AccountInfo(status=REJECTED, message="The token was rejected by the host.", **base)
        limited = self._limit_message(spec.id)
        if limited is not None:
            return AccountInfo(status=LIMITED, message=limited, **base)
        try:
            found = await with_deadline(spec.id, provider.account())
        except RateLimited as e:
            self._note_rate_limit(spec.id, e)
            return AccountInfo(status=LIMITED, message=str(e), **base)
        except ProviderError as e:
            if getattr(provider, "token_rejected", False):
                return AccountInfo(status=REJECTED, message="The token was rejected by the host.", **base)
            return AccountInfo(status=ERROR, message=str(e), **base)
        except Exception:
            return AccountInfo(status=ERROR, message="unexpected error", **base)
        if found is None:
            return AccountInfo(status=ANONYMOUS, **base)
        can, hint = star_fork_hint(spec.kind, found.scopes)
        return AccountInfo(status=SIGNED_IN, login=found.login, scopes=found.scopes, rate=found.rate,
                           can_star_fork=can, hint=hint, **base)

    async def accounts(self, *, refresh: bool = False) -> list[AccountInfo]:
        """One entry per registered host, in registry order."""
        return list(await asyncio.gather(*(self.account(h, refresh=refresh) for h in registry().ids)))

    # ------------------------------------------------------------ write actions (never retried)

    async def _require_signed_in(self, host: str, slug: str):
        """The provider to write with, or an error. Only a real, signed-in, not rate-limited host passes."""
        spec, provider = registry().get(host), self.providers.get(host)
        if spec is None or provider is None:
            raise ProviderError(host, NOT_CONFIGURED)
        if not registry().slug_ok(host, slug):
            raise ProviderError(host, "invalid repository name")
        limited = self._limit_message(host)
        if limited is not None:
            raise RateLimited(host, limited)
        info = await self.account(host)
        if info.status != SIGNED_IN:
            raise ActionDenied(host, f"not signed in ({info.status}); see the Accounts page")
        return provider

    def _drop_repo_cache(self, host: str, slug: str) -> None:
        self._starred.pop(f"{host}@{_domain(host)}:{slug.lower()}", None)
        for kind in ("repo", "detail"):
            self.cache.delete(f"{kind}:{host}@{_domain(host)}:{slug.lower()}")

    async def starred(self, host: str, slug: str) -> bool | None:
        """Whether the signed-in account starred the repository; None when unknown (never raises)."""
        key = f"{host}@{_domain(host)}:{slug.lower()}"
        hit = self._starred.get(key)
        if hit is not None and self._clock() < hit[0]:
            return hit[1]
        try:
            provider = await self._require_signed_in(host, slug)
            state = bool(await with_deadline(host, provider.starred(slug)))
        except RateLimited as e:
            if self._limit_message(host) is None:
                self._note_rate_limit(host, e)
            return None
        except Exception:
            return None
        self._starred[key] = (self._clock() + STARRED_TTL, state)
        return state

    async def set_star(self, host: str, slug: str, star: bool) -> None:
        """Star or unstar once. Already starred / not starred counts as success. Logged either way."""
        action = "star" if star else "unstar"
        await self._act(host, slug, action, lambda p: getattr(p, action)(slug))

    async def fork(self, host: str, slug: str) -> ForkResult:
        return await self._act(host, slug, "fork", lambda p: p.fork(slug))

    async def _act(self, host: str, slug: str, action: str, run):
        try:
            provider = await self._require_signed_in(host, slug)
            result = await with_deadline(host, run(provider))  # exactly one attempt
        except RateLimited as e:
            if self._limit_message(host) is None:  # a fresh limit from the host, not our own pause
                self._note_rate_limit(host, e)
            self.actions.add(host, slug, action, False, str(e))
            raise
        except ProviderError as e:
            self.actions.add(host, slug, action, False, str(e))
            raise
        except Exception:
            self.actions.add(host, slug, action, False, "unexpected error")
            raise ProviderError(host, "unexpected error") from None
        self.actions.add(host, slug, action, True, f"forked to {result.slug}" if action == "fork" else "done")
        self._drop_repo_cache(host, slug)
        return result

    def recent_actions(self, limit: int = 20) -> list[ActionEntry]:
        return self.actions.recent(limit)
