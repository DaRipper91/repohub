from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

from repohub.core.accounts import ProviderAccount, clean_login
from repohub.core.models import MAX_STARS, Asset, Release, Repo, SearchFilters, parse_arch
from repohub.core.textsafe import clean_text
from repohub.core.providers.base import NotFound, ProviderError, RateLimited, guard_parse, safe_url, valid_slug

README_NAMES = ("README.md", "README.markdown", "README.rst", "README.txt", "README")
MAX_LIMIT = 50
MAX_SLUG = 200
MAX_WORDS = 6
MAX_BODY_BYTES = 4 * 1024 * 1024  # largest response body read from a host


def _count(value, default: int | None = None) -> int:
    """A real, finite integer clamped to 0..MAX_STARS. bool, strings, None, non-integral or non-finite floats are
    invalid: ValueError (a malformed response) unless a default is given. Note a hostile configured host can still
    display any stars it likes within the range: the trust model is that the hosts loader decides which hosts to trust."""
    try:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("count")
        if isinstance(value, float) and not (math.isfinite(value) and value.is_integer()):
            raise ValueError("count")
        return max(0, min(int(value), MAX_STARS))
    except (ValueError, OverflowError):
        if default is None:
            raise ValueError("count") from None
        return default


def _utc(value) -> str:
    """Normalise an ISO timestamp with any offset to UTC YYYY-MM-DDTHH:MM:SSZ; unparseable gives ''."""
    if not isinstance(value, str) or not value:
        return ""
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return ""
    try:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, ValueError):
        return ""


class ForgejoProvider:
    def __init__(self, host_id: str, base_url: str, token: str | None = None):
        self.host = host_id
        try:
            u = urlparse(base_url)
            hostname, _ = u.hostname, u.port
        except ValueError:
            raise ValueError("invalid base URL") from None
        if u.scheme != "https" or not hostname:
            raise ValueError("base URL must be an https URL with a host")
        if u.username is not None or u.password is not None or "@" in u.netloc:
            raise ValueError("base URL must not contain credentials")
        if u.query or u.fragment or "?" in base_url or "#" in base_url:
            raise ValueError("base URL must not contain a query or fragment")
        self._domain = hostname
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

    def _own_url(self, value) -> str:
        """A repo page URL is trusted only when it is on this provider's own hostname."""
        url = safe_url(value)
        try:
            return url if url and (urlparse(url).hostname or "").lower() == self._domain.lower() else ""
        except ValueError:
            return ""

    def _to_repo(self, item: dict) -> Repo:
        raw = item["full_name"]
        if not isinstance(raw, str):
            raise TypeError("full_name")
        slug = clean_text(raw)
        if len(slug) > MAX_SLUG or not valid_slug(slug, "github"):
            raise ValueError("full_name")
        return Repo(
            host=self.host, slug=slug, url=self._own_url(item.get("html_url")) or f"https://{self._domain}/{slug}",
            description=clean_text(item.get("description")), stars=_count(item["stars_count"]), language=clean_text(item.get("language")),
            license="", topics=tuple(clean_text(t) for t in (item.get("topics") or ())), pushed_at=_utc(item.get("updated_at")),
            archived=bool(item.get("archived")), forks=_count(item.get("forks_count"), 0), homepage=safe_url(item.get("website")),
            fork=bool(item.get("fork")),
        )

    @staticmethod
    def _slug_ok(item) -> bool:
        raw = item["full_name"]  # missing key / non-object item stay malformed-response errors
        if not isinstance(raw, str):
            raise TypeError("full_name")
        slug = clean_text(raw)
        return len(slug) <= MAX_SLUG and valid_slug(slug, "github")

    async def _send(self, path: str, params: dict | None) -> httpx.Response:
        """GET with a body-size cap. Only 2xx bodies are read; the connection is closed on refusal."""
        try:
            async with self._client.stream("GET", path, params=params) as resp:
                if not 200 <= resp.status_code < 300:
                    return httpx.Response(resp.status_code, headers={"retry-after": resp.headers.get("retry-after", "")})
                declared = resp.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
                    raise ProviderError(self.host, "response too large")
                body = bytearray()
                async for chunk in resp.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_BODY_BYTES:
                        raise ProviderError(self.host, "response too large")
                return httpx.Response(resp.status_code, content=bytes(body))
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
        # Forgejo's /repos/search treats the whole q as ONE keyword (verified against codeberg.org on 2026-09-21:
        # "wayland" and "terminal" match, "wayland terminal" matches nothing). So with several words we send the
        # longest one and require every word client-side; this filter can shrink a page below `limit`.
        words = [w for w in (clean_text(w) for w in query.split()[:MAX_WORDS]) if w]
        text = max(words, key=len) if words else ""
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
        data = data[:params["limit"]]  # never map more than was asked for
        repos = [self._to_repo(i) for i in data if self._slug_ok(i)]  # invalid names are dropped, not fatal
        if len(words) > 1:
            folded = [w.casefold() for w in words]

            def has_all(r: Repo) -> bool:
                hay = " ".join((r.slug, r.description, *r.topics)).casefold()
                return all(w in hay for w in folded)

            repos = [r for r in repos if has_all(r)]
        if client_topic:
            want = client_topic.strip().casefold()
            repos = [r for r in repos if want in (t.casefold() for t in r.topics)]
        lang = (filters.language or "").strip().casefold()
        if lang:
            repos = [r for r in repos if r.language.casefold() == lang]
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
                size = a.get("size")
                size = size if isinstance(size, int) and not isinstance(size, bool) and size >= 0 else 0
                assets.append(Asset(clean_text(a["name"]), size, url, parse_arch(a["name"])))
        published = j.get("published_at")
        return Release(clean_text(j["tag_name"]), clean_text(published) if isinstance(published, str) else None, tuple(assets))

    @guard_parse
    async def account(self) -> ProviderAccount | None:
        """Who the token belongs to (None when there is no token). Forgejo reports no scopes or limits."""
        if "Authorization" not in self._client.headers:
            return None
        resp = await self._get("/user")
        if resp is None:
            raise ProviderError(self.host, "unexpected response")
        return ProviderAccount(clean_login(resp.json()["login"]))
