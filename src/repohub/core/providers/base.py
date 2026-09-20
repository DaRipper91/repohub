from __future__ import annotations

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
