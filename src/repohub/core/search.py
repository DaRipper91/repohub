from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone

from repohub.core.hosts import registry
from repohub.core.models import Repo, SearchFilters
from repohub.core.providers.base import ProviderError


@dataclass
class SearchResult:
    repos: list[Repo] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    stale: bool = False

    def to_dict(self) -> dict:
        return {"repos": [r.to_dict() for r in self.repos], "errors": self.errors, "stale": self.stale}

    @classmethod
    def from_dict(cls, d: dict) -> "SearchResult":
        return cls([Repo.from_dict(r) for r in d["repos"]], dict(d["errors"]), bool(d.get("stale")))


def _keep(repo: Repo, f: SearchFilters) -> bool:
    if repo.stars < f.min_stars:
        return False
    if repo.archived and not f.include_archived:
        return False
    if f.pushed_after and repo.pushed_at[:10] < f.pushed_after:
        return False
    if f.hide_forks and repo.fork:
        return False
    return True


def _ordered(repos, sort: str) -> list[Repo]:
    rank = registry().rank
    tied = sorted(repos, key=lambda r: (rank(r.host), r.slug.lower()))
    if sort == "updated":
        return sorted(tied, key=lambda r: r.pushed_at, reverse=True)
    if sort == "forks":
        return sorted(tied, key=lambda r: r.forks, reverse=True)
    return sorted(tied, key=lambda r: r.stars, reverse=True)


async def search_all(providers: dict, query: str, filters: SearchFilters, now: datetime | None = None) -> SearchResult:
    if filters.updated_within_days:
        now = now or datetime.now(timezone.utc)
        cutoff = (now - timedelta(days=min(filters.updated_within_days, 36500))).date().isoformat()
        filters = replace(filters, pushed_after=cutoff)
    hosts = [h for h in filters.hosts if h in providers]
    outcomes = await asyncio.gather(*(providers[h].search(query, filters) for h in hosts), return_exceptions=True)
    errors: dict[str, str] = {}
    best: dict[str, Repo] = {}
    rank = registry().rank
    for host, out in zip(hosts, outcomes):
        if isinstance(out, ProviderError):
            errors[host] = str(out)
            continue
        if isinstance(out, BaseException):
            errors[host] = "unexpected error"
            continue
        for repo in out:
            if not _keep(repo, filters):
                continue
            cur = best.get(repo.slug.lower())
            if cur is None or repo.stars > cur.stars or (repo.stars == cur.stars and rank(repo.host) < rank(cur.host)):
                best[repo.slug.lower()] = repo
    repos = _ordered(best.values(), filters.sort)
    return SearchResult(repos, errors)
