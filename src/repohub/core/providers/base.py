from __future__ import annotations

import functools
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
