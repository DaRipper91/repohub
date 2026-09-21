from __future__ import annotations

import functools
import time
from dataclasses import dataclass
import re
import unicodedata
from urllib.parse import urlparse

_SLUG = re.compile(r"^[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)+$")


class ProviderError(Exception):
    def __init__(self, host: str, message: str, reset_at: int | None = None):
        super().__init__(message)
        self.host = host
        self.reset_at = reset_at


class NotFound(ProviderError):
    pass


class ActionDenied(ProviderError):
    """The host refused a write (no sign-in, or the token lacks permission)."""


class Conflict(ProviderError):
    """The write clashes with existing state (for example a fork name already taken)."""


@dataclass(frozen=True)
class ForkResult:
    slug: str
    url: str


class RateLimited(ProviderError):
    pass


# control, format (bidi, zero-width, soft hyphen), surrogate, private-use and line/paragraph separators
_BAD_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Zl", "Zp"})


def safe_url(value: str | None) -> str:
    """Return the stripped value only if it is a plain http(s) URL with a host and no whitespace/control chars."""
    if not value:
        return ""
    v = value.strip()
    if not v or any(c.isspace() or ord(c) < 32 or ord(c) == 127 or unicodedata.category(c) in _BAD_CATEGORIES
                    for c in v):
        return ""
    try:
        u = urlparse(v)
        host = u.hostname
        userinfo = u.username is not None or u.password is not None or "@" in u.netloc
    except ValueError:
        return ""
    return v if u.scheme.lower() in ("http", "https") and host and not userinfo else ""


def valid_slug(slug: str, host: str = "github") -> bool:
    if not _SLUG.fullmatch(slug):
        return False
    parts = slug.split("/")
    if any(p in (".", "..") for p in parts):
        return False
    return len(parts) == 2 if host == "github" else True


def guard_parse(fn):
    """Map malformed-response exceptions to ProviderError; let ProviderError/httpx errors pass."""
    @functools.wraps(fn)
    async def wrapper(self, *args, **kwargs):
        try:
            return await fn(self, *args, **kwargs)
        except (KeyError, TypeError, ValueError, AttributeError, IndexError, OverflowError, RecursionError):
            raise ProviderError(self.host, "unexpected response") from None
    return wrapper


MAX_ACTION_BODY = 1_000_000
NO_TOKEN = "not signed in: set a token for this host (see the Accounts page)"


async def call(client, host: str, method: str, path: str, *, ok: tuple[int, ...], params: dict | None = None,
               auth_header: str = "Authorization") -> "httpx.Response":
    """One authenticated request for a write (or star-state read): a single attempt, never retried.

    Redirects are not followed, a rejected token is never dropped or swapped for anonymous access, and
    error bodies never reach a message. Failures map to typed errors; ``ok`` lists the accepted codes.
    """
    import httpx

    if auth_header not in client.headers:
        raise ActionDenied(host, NO_TOKEN)
    try:
        async with client.stream(method, path, params=params) as resp:
            declared = resp.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > MAX_ACTION_BODY:
                raise ProviderError(host, "response too large")
            body = bytearray()
            async for chunk in resp.aiter_bytes():
                body += chunk
                if len(body) > MAX_ACTION_BODY:
                    raise ProviderError(host, "response too large")
            code, headers = resp.status_code, resp.headers
            out = httpx.Response(code, headers=dict(headers), content=bytes(body))
    except httpx.HTTPError as e:
        raise ProviderError(host, "network error") from e
    if code in ok:
        return out
    retry = headers.get("retry-after", "")
    reset = headers.get("x-ratelimit-reset", "")
    if code == 429 or (code == 403 and headers.get("x-ratelimit-remaining") == "0"):
        at = int(reset) if reset.isdigit() else (int(time.time()) + int(retry) if retry.isdigit() else None)
        raise RateLimited(host, "rate limited", at)
    if code == 401:
        raise ActionDenied(host, "token rejected")
    if code == 403:
        raise ActionDenied(host, "permission denied: the token may lack the needed scope (see the Accounts page)")
    if code == 404:
        raise NotFound(host, "repository not found (or the token cannot see it)")
    if code in (409, 422):
        raise Conflict(host, "a repository with that name already exists in your account, or the fork is not allowed")
    raise ProviderError(host, f"HTTP {code}")


def fork_result(host_web: str, slug_value, url_value) -> ForkResult:
    """A fork's slug and link, validated; falls back to a link built from the slug."""
    slug = slug_value if isinstance(slug_value, str) and _SLUG.fullmatch(slug_value) else ""
    url = safe_url(url_value if isinstance(url_value, str) else None)
    if not slug:
        raise ValueError("bad fork slug")  # guard_parse turns this into "unexpected response"
    return ForkResult(slug, url or f"{host_web}/{slug}")
