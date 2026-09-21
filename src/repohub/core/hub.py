from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import asdict, dataclass
from typing import Callable

from repohub.core.browse import Shelf, ShelfEntry
from repohub.core.cache import Cache
from repohub.core.models import Release, Repo, SearchFilters
from repohub.core.providers.base import NotFound, ProviderError, RateLimited
from repohub.core.search import SearchResult, search_all
from repohub.core.store import Favorites

SEARCH_TTL = 600
DETAIL_TTL = 3600
DEGRADED_TTL = 60
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
    return Repo(host=entry.host, slug=entry.slug, url=f"https://{entry.host}.com/{entry.slug}",
                description=snap.description if snap else "", stars=snap.stars if snap else 0,
                language=snap.language if snap else "", license=snap.license if snap else "",
                topics=(), pushed_at=snap.pushed_at if snap else "", archived=False, forks=0,
                homepage="", fork=False)


class Hub:
    def __init__(self, providers: dict, cache: Cache, favorites: Favorites, *,
                 clock: Callable[[], float] = time.time):
        self.providers, self.cache, self.favorites = providers, cache, favorites
        self._clock = clock
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
        digest = hashlib.sha256(json.dumps([query, asdict(filters)], sort_keys=True).encode()).hexdigest()
        key = f"search:{digest}"
        hit = self.cache.get(key)
        if hit is not None:
            return SearchResult.from_dict(hit)
        result = await search_all(self.providers, query, filters)
        if not result.errors:
            self.cache.set(key, result.to_dict(), SEARCH_TTL)
        elif not result.repos:
            stale = self.cache.get(key, allow_stale=True)
            if stale is not None:
                old = SearchResult.from_dict(stale)
                return SearchResult(old.repos, result.errors, stale=True)
        return result

    async def shelf(self, shelf: Shelf) -> SearchResult:
        if shelf.curated:
            page = await self.curated_page(shelf, 0, CURATED_PAGE)
            return SearchResult([item.repo for item in page.items], page.errors)
        return await self.search(shelf.query, shelf.filters())

    async def repo_summary(self, host: str, slug: str) -> Repo:
        provider = self.providers.get(host)
        if provider is None:
            raise ProviderError(host, "unknown host")
        key = f"repo:{host}:{slug.lower()}"
        hit = self.cache.get(key)
        if hit is not None:
            return Repo.from_dict(hit)
        repo = await provider.repo(slug)
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
            raise ProviderError(host, "unknown host")
        key = f"detail:{host}:{slug.lower()}"
        hit = self.cache.get(key)
        if hit is not None:
            return Detail.from_dict(hit)
        try:
            repo, readme, release = await asyncio.gather(
                provider.repo(slug), provider.readme(slug), provider.latest_release(slug), return_exceptions=True)
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
                    fresh = await provider.repo(repo.slug)
                self.favorites.update(fresh)
            except ProviderError:
                self.favorites.mark_checked(repo.key)  # retry at most once per max_age

        await asyncio.gather(*(one(r) for r in self.favorites.stale(max_age)))
