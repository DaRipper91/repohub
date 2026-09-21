from __future__ import annotations

import time
from urllib.parse import quote

import httpx

from repohub.core.accounts import ProviderAccount, clean_login, clean_scopes, parse_rate
from repohub.core.models import Asset, Release, Repo, SearchFilters, parse_arch
from repohub.core.textsafe import clean_text
from repohub.core.providers.base import ForkResult, NotFound, call, fork_result, ProviderError, RateLimited, guard_parse, safe_url, valid_slug

README_NAMES = ("README.md", "README.markdown", "README.rst", "README.txt", "README")


def _to_repo(item: dict, language: str = "") -> Repo:
    return Repo(
        host="gitlab", slug=clean_text(item["path_with_namespace"]), url=safe_url(item["web_url"]) or f'https://gitlab.com/{item["path_with_namespace"]}', description=clean_text(item.get("description")),
        stars=item.get("star_count", 0), language=clean_text(language), license=clean_text((item.get("license") or {}).get("name")),
        topics=tuple(clean_text(t) for t in (item.get("topics") or item.get("tag_list") or ())), pushed_at=item.get("last_activity_at") or "",
        archived=bool(item.get("archived")), forks=item.get("forks_count", 0), homepage="",
        fork=bool(item.get("forked_from_project")),
    )


class GitLabProvider:
    host = "gitlab"

    def __init__(self, token: str | None = None, base_url: str = "https://gitlab.com/api/v4"):
        headers = {"User-Agent": "repohub"}
        if token:
            headers["PRIVATE-TOKEN"] = token
        self.token_rejected = False
        self._client = httpx.AsyncClient(base_url=base_url, headers=headers, timeout=15)

    async def aclose(self) -> None:
        await self._client.aclose()

    def _check_slug(self, slug: str) -> str:
        if not valid_slug(slug, "gitlab"):
            raise ProviderError(self.host, "invalid repository name")
        return quote(slug, safe="")

    async def _send(self, path: str, params: dict | None) -> httpx.Response:
        try:
            return await self._client.get(path, params=params)
        except httpx.HTTPError as e:
            raise ProviderError(self.host, "network error") from e

    def _drop_token(self) -> bool:
        """A rejected token falls back to anonymous access for this host."""
        if "PRIVATE-TOKEN" not in self._client.headers:
            return False
        del self._client.headers["PRIVATE-TOKEN"]
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
        params: dict = {"order_by": "last_activity_at" if filters.sort == "updated" else "star_count", "sort": "desc", "per_page": per_page, "license": "true"}
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
            raise NotFound(self.host, "repository not found")
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
                return clean_text(resp.text, multiline=True)
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
            Asset(clean_text(a["name"]), 0, a.get("direct_asset_url") or a["url"], parse_arch(a["name"]))
            for a in (j.get("assets") or {}).get("links", [])
        )
        return Release(clean_text(j["tag_name"]), j.get("released_at"), assets)

    @guard_parse
    async def account(self) -> ProviderAccount | None:
        """Who the token belongs to and its scopes (None when there is no token)."""
        if "PRIVATE-TOKEN" not in self._client.headers:
            return None
        resp = await self._get("/user")
        if resp is None:
            raise ProviderError(self.host, "unexpected response")
        login, rate = clean_login(resp.json()["username"]), parse_rate(resp.headers, "ratelimit")
        scopes = None
        try:  # only personal access tokens answer this; anything else leaves the scopes unknown
            own = await self._send("/personal_access_tokens/self", None)  # never drops the token
            if own.status_code == 200 and isinstance(own.json().get("scopes"), list):
                scopes = clean_scopes(own.json()["scopes"])
        except (ProviderError, ValueError, AttributeError):
            pass
        return ProviderAccount(login, scopes, rate)

    async def _call(self, method: str, path: str, ok: tuple[int, ...], params: dict | None = None):
        return await call(self._client, self.host, method, path, ok=ok, params=params, auth_header="PRIVATE-TOKEN")

    @guard_parse
    async def starred(self, slug: str) -> bool:
        """GitLab has no direct check: look for the signed-in username among the project's starrers."""
        pid = self._check_slug(slug)
        me = clean_login((await self._call("GET", "/user", (200,))).json()["username"])
        if not me:
            raise ProviderError(self.host, "unexpected response")
        found = (await self._call("GET", f"/projects/{pid}/starrers", (200,), {"search": me, "per_page": 100})).json()
        return any(isinstance(s, dict) and isinstance(s.get("user"), dict) and s["user"].get("username") == me
                   for s in found)

    async def star(self, slug: str) -> None:
        await self._call("POST", f"/projects/{self._check_slug(slug)}/star", (200, 201, 304))

    async def unstar(self, slug: str) -> None:
        await self._call("POST", f"/projects/{self._check_slug(slug)}/unstar", (200, 201, 304))

    @guard_parse
    async def fork(self, slug: str) -> ForkResult:
        resp = await self._call("POST", f"/projects/{self._check_slug(slug)}/fork", (200, 201, 202))
        j = resp.json()
        return fork_result("https://gitlab.com", j["path_with_namespace"], j.get("web_url"))
