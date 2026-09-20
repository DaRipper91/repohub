from __future__ import annotations

import time
from urllib.parse import quote

import httpx

from repohub.core.models import Asset, Release, Repo, SearchFilters, parse_arch
from repohub.core.providers.base import ProviderError, RateLimited, guard_parse, valid_slug

README_NAMES = ("README.md", "README.markdown", "README.rst", "README.txt", "README")


def _to_repo(item: dict, language: str = "") -> Repo:
    return Repo(
        host="gitlab", slug=item["path_with_namespace"], url=item["web_url"], description=item.get("description") or "",
        stars=item.get("star_count", 0), language=language, license=(item.get("license") or {}).get("name") or "",
        topics=tuple(item.get("topics") or item.get("tag_list") or ()), pushed_at=item.get("last_activity_at") or "",
        archived=bool(item.get("archived")), forks=item.get("forks_count", 0), homepage="",
    )


class GitLabProvider:
    host = "gitlab"

    def __init__(self, token: str | None = None, base_url: str = "https://gitlab.com/api/v4"):
        headers = {"User-Agent": "repohub"}
        if token:
            headers["PRIVATE-TOKEN"] = token
        self._client = httpx.AsyncClient(base_url=base_url, headers=headers, timeout=15)

    async def aclose(self) -> None:
        await self._client.aclose()

    def _check_slug(self, slug: str) -> str:
        if not valid_slug(slug, "gitlab"):
            raise ProviderError(self.host, "invalid repository name")
        return quote(slug, safe="")

    async def _get(self, path: str, params: dict | None = None) -> httpx.Response | None:
        try:
            resp = await self._client.get(path, params=params)
        except httpx.HTTPError as e:
            raise ProviderError(self.host, "network error") from e
        if resp.status_code == 429:
            retry = resp.headers.get("retry-after", "")
            raise RateLimited(self.host, "rate limited", int(time.time()) + int(retry) if retry.isdigit() else None)
        if resp.status_code == 401:
            raise ProviderError(self.host, "token rejected")
        if resp.status_code == 404:
            return None
        if resp.status_code >= 300:  # redirects are deliberately not followed
            raise ProviderError(self.host, f"HTTP {resp.status_code}")
        return resp

    @guard_parse
    async def search(self, query: str, filters: SearchFilters, per_page: int = 30) -> list[Repo]:
        params: dict = {"order_by": "star_count", "sort": "desc", "per_page": per_page, "license": "true"}
        if query.strip():
            params["search"] = query.strip()
        if not filters.include_archived:
            params["archived"] = "false"
        if filters.language:
            params["with_programming_language"] = filters.language
        if filters.topic:
            params["topic"] = filters.topic
        if filters.pushed_after:
            params["last_activity_after"] = f"{filters.pushed_after}T00:00:00Z"
        resp = await self._get("/projects", params)
        if resp is None:
            raise ProviderError(self.host, "unexpected response")
        return [_to_repo(i, filters.language or "") for i in resp.json()]

    @guard_parse
    async def repo(self, slug: str) -> Repo:
        enc = self._check_slug(slug)
        resp = await self._get(f"/projects/{enc}", {"license": "true"})
        if resp is None:
            raise ProviderError(self.host, "repository not found")
        item = resp.json()
        language = ""
        try:
            langs = await self._get(f"/projects/{item['id']}/languages")
            if langs is not None and langs.json():
                data = langs.json()
                language = max(data, key=data.get)
        except (ProviderError, KeyError, TypeError, ValueError, AttributeError):
            language = ""
        return _to_repo(item, language)

    @guard_parse
    async def readme(self, slug: str) -> str | None:
        enc = self._check_slug(slug)
        for name in README_NAMES:
            resp = await self._get(f"/projects/{enc}/repository/files/{quote(name, safe='')}/raw", {"ref": "HEAD"})
            if resp is not None:
                return resp.text
        return None

    @guard_parse
    async def latest_release(self, slug: str) -> Release | None:
        enc = self._check_slug(slug)
        resp = await self._get(f"/projects/{enc}/releases", {"per_page": 1})
        items = resp.json() if resp is not None else []
        if not items:
            return None
        j = items[0]
        assets = tuple(
            Asset(a["name"], 0, a.get("direct_asset_url") or a["url"], parse_arch(a["name"]))
            for a in (j.get("assets") or {}).get("links", [])
        )
        return Release(j["tag_name"], j.get("released_at"), assets)
