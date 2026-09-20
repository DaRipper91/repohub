from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import asdict, dataclass

from repohub.core.browse import Shelf
from repohub.core.cache import Cache
from repohub.core.models import Release, Repo, SearchFilters
from repohub.core.providers.base import ProviderError, RateLimited
from repohub.core.search import SearchResult, search_all
from repohub.core.store import Favorites

SEARCH_TTL = 600
DETAIL_TTL = 3600
DEGRADED_TTL = 60
REFRESH_CONCURRENCY = 8


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


class Hub:
    def __init__(self, providers: dict, cache: Cache, favorites: Favorites):
        self.providers, self.cache, self.favorites = providers, cache, favorites

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
        return await self.search(shelf.query, shelf.filters())

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
        except RateLimited:
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
                pass

        await asyncio.gather(*(one(r) for r in self.favorites.stale(max_age)))
