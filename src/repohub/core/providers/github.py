from __future__ import annotations

import httpx

from repohub.core.models import Asset, Release, Repo, SearchFilters, parse_arch
from repohub.core.providers.base import ProviderError, RateLimited, valid_slug


def _to_repo(item: dict) -> Repo:
    lic = (item.get("license") or {}).get("spdx_id") or ""
    if lic == "NOASSERTION":
        lic = "other"
    return Repo(
        host="github", slug=item["full_name"], url=item["html_url"], description=item.get("description") or "",
        stars=item["stargazers_count"], language=item.get("language") or "", license=lic,
        topics=tuple(item.get("topics") or ()), pushed_at=item.get("pushed_at") or "",
        archived=bool(item.get("archived")), forks=item.get("forks_count", 0), homepage=item.get("homepage") or "",
    )


class GitHubProvider:
    host = "github"

    def __init__(self, token: str | None = None, base_url: str = "https://api.github.com"):
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "repohub"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.AsyncClient(base_url=base_url, headers=headers, timeout=15)

    async def aclose(self) -> None:
        await self._client.aclose()

    def _check_slug(self, slug: str) -> None:
        if not valid_slug(slug, "github"):
            raise ProviderError(self.host, "invalid repository name")

    async def _get(self, path: str, params: dict | None = None, accept: str | None = None) -> httpx.Response | None:
        try:
            resp = await self._client.get(path, params=params, headers={"Accept": accept} if accept else None)
        except httpx.HTTPError as e:
            raise ProviderError(self.host, "network error") from e
        limited = resp.headers.get("x-ratelimit-remaining") == "0"
        if resp.status_code == 429 or (resp.status_code == 403 and limited):
            reset = resp.headers.get("x-ratelimit-reset", "")
            raise RateLimited(self.host, "rate limited", int(reset) if reset.isdigit() else None)
        if resp.status_code == 401:
            raise ProviderError(self.host, "token rejected")
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise ProviderError(self.host, f"HTTP {resp.status_code}")
        return resp

    async def search(self, query: str, filters: SearchFilters, per_page: int = 30) -> list[Repo]:
        parts = [query.strip()] if query.strip() else []
        if filters.min_stars:
            parts.append(f"stars:>={filters.min_stars}")
        if filters.language:
            parts.append('language:"%s"' % filters.language.replace('"', ""))
        if filters.topic:
            parts.append(f"topic:{filters.topic}")
        if filters.pushed_after:
            parts.append(f"pushed:>={filters.pushed_after}")
        if not filters.include_archived:
            parts.append("archived:false")
        resp = await self._get("/search/repositories",
                               {"q": " ".join(parts), "sort": "stars", "order": "desc", "per_page": per_page})
        return [_to_repo(i) for i in resp.json()["items"]]

    async def repo(self, slug: str) -> Repo:
        self._check_slug(slug)
        resp = await self._get(f"/repos/{slug}")
        if resp is None:
            raise ProviderError(self.host, "repository not found")
        return _to_repo(resp.json())

    async def readme(self, slug: str) -> str | None:
        self._check_slug(slug)
        resp = await self._get(f"/repos/{slug}/readme", accept="application/vnd.github.raw+json")
        return resp.text if resp is not None else None

    async def latest_release(self, slug: str) -> Release | None:
        self._check_slug(slug)
        resp = await self._get(f"/repos/{slug}/releases/latest")
        if resp is None:
            return None
        j = resp.json()
        assets = tuple(Asset(a["name"], a.get("size", 0), a["browser_download_url"], parse_arch(a["name"]))
                       for a in j.get("assets", []))
        return Release(j["tag_name"], j.get("published_at"), assets)
