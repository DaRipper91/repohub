from __future__ import annotations

import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

from repohub.core.models import Asset, Release, Repo, SearchFilters, parse_arch
from repohub.core.textsafe import clean_text
from repohub.core.providers.base import NotFound, ProviderError, RateLimited, guard_parse, safe_url, valid_slug

README_NAMES = ("README.md", "README.markdown", "README.rst", "README.txt", "README")
MAX_LIMIT = 50


def _utc(value) -> str:
    """Normalise an ISO timestamp with any offset to UTC YYYY-MM-DDTHH:MM:SSZ; unparseable gives ''."""
    if not isinstance(value, str) or not value:
        return ""
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ForgejoProvider:
    def __init__(self, host_id: str, base_url: str, token: str | None = None):
        self.host = host_id
        self._domain = urlparse(base_url).hostname or ""
        headers = {"User-Agent": "repohub"}
        if token:
            headers["Authorization"] = f"token {token}"
        self.token_rejected = False
        self._client = httpx.AsyncClient(base_url=base_url, headers=headers, timeout=15)

    async def aclose(self) -> None:
        await self._client.aclose()

    def _check_slug(self, slug: str) -> str:
        if not valid_slug(slug, "github"):  # exactly two segments
            raise ProviderError(self.host, "invalid repository name")
        return slug

    def _to_repo(self, item: dict) -> Repo:
        slug = item["full_name"]
        if not isinstance(slug, str):
            raise TypeError("full_name")
        return Repo(
            host=self.host, slug=clean_text(slug), url=safe_url(item.get("html_url")) or f"https://{self._domain}/{slug}",
            description=clean_text(item.get("description")), stars=int(item["stars_count"]), language=clean_text(item.get("language")),
            license="", topics=tuple(clean_text(t) for t in (item.get("topics") or ())), pushed_at=_utc(item.get("updated_at")),
            archived=bool(item.get("archived")), forks=int(item.get("forks_count") or 0), homepage=safe_url(item.get("website")),
            fork=bool(item.get("fork")),
        )

    async def _send(self, path: str, params: dict | None) -> httpx.Response:
        try:
            return await self._client.get(path, params=params)
        except httpx.HTTPError as e:
            raise ProviderError(self.host, "network error") from e

    def _drop_token(self) -> bool:
        """A rejected token falls back to anonymous access for this host."""
        if "Authorization" not in self._client.headers:
            return False
        del self._client.headers["Authorization"]
        self.token_rejected = True
        return True

    async def _get(self, path: str, params: dict | None = None) -> httpx.Response | None:
        resp = await self._send(path, params)
        if resp.status_code == 401 and self._drop_token():
            resp = await self._send(path, params)  # one anonymous retry, never a loop
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
        text = query.strip()
        params: dict = {"sort": "updated" if filters.sort == "updated" else "stars", "order": "desc",
                        "limit": max(1, min(per_page, MAX_LIMIT))}
        client_topic = None
        if text:
            params["q"] = text
            client_topic = filters.topic
        elif filters.topic:
            params["q"] = filters.topic
            params["topic"] = "true"
        if filters.hide_forks:
            params["mode"] = "source"
        if not filters.include_archived:
            params["archived"] = "false"
        resp = await self._get("/repos/search", params)
        if resp is None:
            raise ProviderError(self.host, "unexpected response")
        data = resp.json()["data"]
        if not isinstance(data, list):
            raise TypeError("data")
        repos = [self._to_repo(i) for i in data]
        if client_topic:
            want = client_topic.strip().lower()
            repos = [r for r in repos if want in (t.lower() for t in r.topics)]
        if filters.language:
            lang = filters.language.strip().lower()
            repos = [r for r in repos if r.language.lower() == lang]
        return repos

    @guard_parse
    async def repo(self, slug: str) -> Repo:
        self._check_slug(slug)
        resp = await self._get(f"/repos/{slug}")
        if resp is None:
            raise NotFound(self.host, "repository not found")
        return self._to_repo(resp.json())

    @guard_parse
    async def readme(self, slug: str) -> str | None:
        self._check_slug(slug)
        for name in README_NAMES:
            resp = await self._get(f"/repos/{slug}/raw/{name}")
            if resp is not None:
                return clean_text(resp.text, multiline=True)
        return None

    @guard_parse
    async def latest_release(self, slug: str) -> Release | None:
        self._check_slug(slug)
        resp = await self._get(f"/repos/{slug}/releases/latest")
        if resp is None:
            return None
        j = resp.json()
        assets = []
        for a in j.get("assets") or ():
            url = safe_url(a.get("browser_download_url"))
            if url:
                assets.append(Asset(clean_text(a["name"]), a.get("size") or 0, url, parse_arch(a["name"])))
        return Release(clean_text(j["tag_name"]), j.get("published_at"), tuple(assets))
