"""Who RepoHub is signed in as on each host. Read-only; nothing here is stored or holds a token."""
from __future__ import annotations

import re
from dataclasses import dataclass

from repohub.core.textsafe import clean_text

MAX_SCOPES = 30
_SCOPE = re.compile(r"^[A-Za-z0-9_:.-]{1,40}$")
_LOGIN = re.compile(r"^[A-Za-z0-9_.@-]{1,100}$")

# status values of AccountInfo
SIGNED_IN, ANONYMOUS, REJECTED, LIMITED, ERROR, UNAVAILABLE = (
    "signed in", "not signed in", "token rejected", "rate limited", "error", "not configured")


@dataclass(frozen=True)
class RateLimit:
    limit: int
    remaining: int
    reset_at: int | None = None


@dataclass(frozen=True)
class ProviderAccount:
    """What one host reports about the token: login, scopes (None = not reported), rate limit."""
    login: str
    scopes: tuple[str, ...] | None = None
    rate: RateLimit | None = None


@dataclass(frozen=True)
class AccountInfo:
    host: str
    name: str
    status: str
    source: str = ""
    login: str = ""
    scopes: tuple[str, ...] | None = None
    rate: RateLimit | None = None
    can_star_fork: str = "unknown"  # yes | no | unknown
    hint: str = ""
    message: str = ""

    def to_dict(self) -> dict:
        return {"host": self.host, "name": self.name, "status": self.status, "token_source": self.source or None,
                "login": self.login or None, "scopes": list(self.scopes) if self.scopes is not None else None,
                "rate_limit": ({"limit": self.rate.limit, "remaining": self.rate.remaining,
                                "reset_at": self.rate.reset_at} if self.rate else None),
                "can_star_fork": self.can_star_fork, "hint": self.hint, "message": self.message}


def clean_login(value) -> str:
    login = clean_text(value if isinstance(value, str) else "")
    return login if _LOGIN.fullmatch(login) else ""


def clean_scopes(values) -> tuple[str, ...]:
    out: list[str] = []
    for v in values or ():
        v = clean_text(v if isinstance(v, str) else "")
        if _SCOPE.fullmatch(v) and v not in out:
            out.append(v)
        if len(out) >= MAX_SCOPES:
            break
    return tuple(out)


def _int(value) -> int | None:
    v = str(value).strip() if value is not None else ""
    return int(v) if v.isascii() and v.isdigit() and len(v) <= 12 else None


def parse_rate(headers, prefix: str) -> RateLimit | None:
    """Rate limit from ``<prefix>-limit/-remaining/-reset`` headers, or None when not all usable."""
    limit, remaining = _int(headers.get(f"{prefix}-limit")), _int(headers.get(f"{prefix}-remaining"))
    if limit is None or remaining is None:
        return None
    return RateLimit(limit, remaining, _int(headers.get(f"{prefix}-reset")))


def star_fork_hint(kind: str, scopes: tuple[str, ...] | None) -> tuple[str, str]:
    """(yes|no|unknown, fix or note) for starring and forking, from what the host reported."""
    if kind == "github":
        if scopes is None:
            return "unknown", "GitHub does not list permissions for this token type; starring and forking may still work."
        if "repo" in scopes or "public_repo" in scopes:
            return "yes", ""
        return "no", "Add the public_repo scope: gh auth refresh -s public_repo (or make a token with it)."
    if kind == "gitlab":
        if scopes is None:
            return "unknown", "GitLab did not report this token's scopes; starring and forking need the api scope."
        if "api" in scopes:
            return "yes", ""
        return "no", "Make a token with the api scope (read_api cannot star or fork)."
    return "unknown", "This host does not report token scopes; starring and forking need write:repository."
