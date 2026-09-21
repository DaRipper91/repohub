from __future__ import annotations

import httpx

from repohub.core.accounts import ProviderAccount, clean_login, clean_scopes, parse_rate
from repohub.core.models import Asset, Release, Repo, SearchFilters, parse_arch
from repohub.core.textsafe import clean_text
from repohub.core.providers.base import ForkResult, NotFound, call, fork_result, ProviderError, RateLimited, guard_parse, safe_url, valid_slug


def _to_repo(item: dict) -> Repo:
    lic = (item.get("license") or {}).get("spdx_id") or ""
    if lic == "NOASSERTION":
        lic = "other"
    return Repo(
        host="github", slug=clean_text(item["full_name"]), url=safe_url(item["html_url"]) or f'https://github.com/{item["full_name"]}', description=clean_text(item.get("description")),
        stars=item["stargazers_count"], language=clean_text(item.get("language")), license=clean_text(lic),
        topics=tuple(clean_text(t) for t in (item.get("topics") or ())), pushed_at=item.get("pushed_at") or "",
        archived=bool(item.get("archived")), forks=item.get("forks_count", 0), homepage=safe_url(item.get("homepage")),
        fork=bool(item.get("fork")),
    )


class GitHubProvider:
    host = "github"

    def __init__(self, token: str | None = None, base_url: str = "https://api.github.com"):
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "repohub"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.token_rejected = False
        self._client = httpx.AsyncClient(base_url=base_url, headers=headers, timeout=15)

    async def aclose(self) -> None:
        await self._client.aclose()

    def _check_slug(self, slug: str) -> None:
        if not valid_slug(slug, "github"):
            raise ProviderError(self.host, "invalid repository name")

    async def _send(self, path: str, params: dict | None, accept: str | None) -> httpx.Response:
        try:
            return await self._client.get(path, params=params, headers={"Accept": accept} if accept else None)
        except httpx.HTTPError as e:
            raise ProviderError(self.host, "network error") from e

    def _drop_token(self) -> bool:
        """A rejected token falls back to anonymous access for this host."""
        if "Authorization" not in self._client.headers:
            return False
        del self._client.headers["Authorization"]
        self.token_rejected = True
        return True

    async def _get(self, path: str, params: dict | None = None, accept: str | None = None) -> httpx.Response | None:
        resp = await self._send(path, params, accept)
        if resp.status_code == 401 and self._drop_token():
            resp = await self._send(path, params, accept)  # one anonymous retry, never a loop
        limited = resp.headers.get("x-ratelimit-remaining") == "0"
        if resp.status_code == 429 or (resp.status_code == 403 and limited):
            reset = resp.headers.get("x-ratelimit-reset", "")
            raise RateLimited(self.host, "rate limited", int(reset) if reset.isdigit() else None)
        if resp.status_code == 401:
            raise ProviderError(self.host, "token rejected")
        if resp.status_code == 404:
            return None
        if resp.status_code >= 300:  # redirects are deliberately not followed
            raise ProviderError(self.host, f"HTTP {resp.status_code}")
        return resp

    @guard_parse
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
        if filters.hide_forks:
            parts.append("fork:false")
        sort = {"stars": "stars", "updated": "updated", "forks": "forks"}.get(filters.sort, "stars")
        resp = await self._get("/search/repositories",
                               {"q": " ".join(parts), "sort": sort, "order": "desc", "per_page": per_page})
        if resp is None:
            raise ProviderError(self.host, "unexpected response")
        return [_to_repo(i) for i in resp.json()["items"]]

    @guard_parse
    async def repo(self, slug: str) -> Repo:
        self._check_slug(slug)
        resp = await self._get(f"/repos/{slug}")
        if resp is None:
            raise NotFound(self.host, "repository not found")
        return _to_repo(resp.json())

    @guard_parse
    async def readme(self, slug: str) -> str | None:
        self._check_slug(slug)
        resp = await self._get(f"/repos/{slug}/readme", accept="application/vnd.github.raw+json")
        return clean_text(resp.text, multiline=True) if resp is not None else None

    @guard_parse
    async def latest_release(self, slug: str) -> Release | None:
        self._check_slug(slug)
        resp = await self._get(f"/repos/{slug}/releases/latest")
        if resp is None:
            return None
        j = resp.json()
        assets = tuple(Asset(clean_text(a["name"]), a.get("size", 0), a["browser_download_url"], parse_arch(a["name"]))
                       for a in j.get("assets", []))
        return Release(clean_text(j["tag_name"]), j.get("published_at"), assets)

    @guard_parse
    async def account(self) -> ProviderAccount | None:
        """Who the token belongs to (None when there is no token). Classic tokens list their scopes."""
        if "Authorization" not in self._client.headers:
            return None
        resp = await self._get("/user")
        if resp is None:
            raise ProviderError(self.host, "unexpected response")
        listed = resp.headers.get("x-oauth-scopes")  # absent for fine-grained tokens
        scopes = None if listed is None else clean_scopes(s.strip() for s in listed.split(","))
        return ProviderAccount(clean_login(resp.json()["login"]), scopes, parse_rate(resp.headers, "x-ratelimit"))

    async def starred(self, slug: str) -> bool:
        self._check_slug(slug)
        try:
            await call(self._client, self.host, "GET", f"/user/starred/{slug}", ok=(204,))
        except NotFound:
            return False
        return True

    async def star(self, slug: str) -> None:
        self._check_slug(slug)
        await call(self._client, self.host, "PUT", f"/user/starred/{slug}", ok=(204, 304))

    async def unstar(self, slug: str) -> None:
        self._check_slug(slug)
        await call(self._client, self.host, "DELETE", f"/user/starred/{slug}", ok=(204, 304))

    @guard_parse
    async def fork(self, slug: str) -> ForkResult:
        self._check_slug(slug)
        resp = await call(self._client, self.host, "POST", f"/repos/{slug}/forks", ok=(202,))
        j = resp.json()
        return fork_result("https://github.com", j["full_name"], j.get("html_url"))

    @guard_parse
    async def starred_repos(self, limit: int = 200) -> list[Repo]:
        """Repositories the signed-in account starred (at most ``limit``, 100 per request)."""
        if "Authorization" not in self._client.headers:
            return []
        out: list[Repo] = []
        for page in range(1, -(-limit // 100) + 1):
            resp = await self._get("/user/starred", {"per_page": 100, "page": page})
            items = resp.json() if resp is not None else []
            if not isinstance(items, list):
                raise TypeError("items")
            out += [_to_repo(i) for i in items[:100]]
            if len(items) < 100:
                break
        return out[:limit]
