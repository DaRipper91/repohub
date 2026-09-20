from __future__ import annotations

import functools
import re

_SLUG = re.compile(r"^[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)+$")


class ProviderError(Exception):
    def __init__(self, host: str, message: str, reset_at: int | None = None):
        super().__init__(message)
        self.host = host
        self.reset_at = reset_at


class RateLimited(ProviderError):
    pass


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
        except (KeyError, TypeError, ValueError, AttributeError, IndexError):
            raise ProviderError(self.host, "unexpected response") from None
    return wrapper
