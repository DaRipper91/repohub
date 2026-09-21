from __future__ import annotations

import re
from dataclasses import dataclass, replace

from repohub.core.models import SORTS, SearchFilters

MAX_STARS = 10_000_000
MAX_DAYS = 36500
HOSTS = ("github", "gitlab")

_LANG = re.compile(r"^[A-Za-z0-9+#._-]{1,40}$")
_TOPIC = re.compile(r"^[a-z0-9][a-z0-9-]{0,49}$")
_KEYS = {"lang": "language", "language": "language", "stars": "stars", "days": "days",
         "host": "host", "sort": "sort", "topic": "topic"}
_FLAGS = {"nofork", "archived"}


@dataclass(frozen=True)
class ParsedQuery:
    text: str
    filters: SearchFilters
    problems: tuple[str, ...] = ()


def _int(value: str, name: str, limit: int) -> int:
    if value.startswith(">="):
        value = value[2:]
    elif value.startswith(">"):
        value = value[1:]
    # ASCII digits only: int() would also accept unicode digits, "1_000" and "+5".
    if not (value.isascii() and value.isdigit()) or len(value) > 12:
        shown = value if len(value) <= 40 else value[:40] + "..."
        raise ValueError(f"{name} needs a whole number, got '{shown}'")
    n = int(value)
    if not 0 <= n <= limit:
        raise ValueError(f"{name} must be between 0 and {limit}")
    return n


def _apply(filters: SearchFilters, key: str, value: str) -> SearchFilters:
    if key == "language":
        if not _LANG.match(value):
            raise ValueError(f"lang has unsupported characters: '{value[:40]}'")
        return replace(filters, language=value)
    if key == "stars":
        return replace(filters, min_stars=_int(value, "stars", MAX_STARS))
    if key == "days":
        return replace(filters, updated_within_days=_int(value, "days", MAX_DAYS) or None)
    if key == "host":
        v = value.lower()
        if v == "both":
            return replace(filters, hosts=HOSTS)
        if v in HOSTS:
            return replace(filters, hosts=(v,))
        raise ValueError("host must be github, gitlab or both")
    if key == "sort":
        v = value.lower()
        if v not in SORTS:
            raise ValueError("sort must be one of: " + ", ".join(SORTS))
        return replace(filters, sort=v)
    if key == "topic":
        v = value.lower()
        if not _TOPIC.match(v):
            raise ValueError(f"topic has unsupported characters: '{value[:40]}'")
        return replace(filters, topic=v)
    raise ValueError(f"unknown key {key}")  # unreachable: only keys in _KEYS get here


def parse_query(raw: str, base: SearchFilters | None = None) -> ParsedQuery:
    filters = base or SearchFilters()
    words: list[str] = []
    problems: list[str] = []
    for token in (raw or "").split():
        low = token.lower()
        if low in _FLAGS:
            filters = replace(filters, hide_forks=True) if low == "nofork" else replace(filters, include_archived=True)
            continue
        key, sep, value = token.partition(":")
        name = _KEYS.get(key.lower()) if sep else None
        if name is None:
            words.append(token)
            continue
        if value == "":
            problems.append(f"{key.lower()}: needs a value")
            continue
        try:
            filters = _apply(filters, name, value)
        except ValueError as e:
            problems.append(str(e))
    return ParsedQuery(" ".join(words), filters, tuple(problems))
