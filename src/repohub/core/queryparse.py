from __future__ import annotations

import re
from dataclasses import dataclass, replace

from repohub.core.hosts import registry
from repohub.core.models import MAX_DAYS, MAX_STARS, SORTS, SearchFilters
from repohub.core.textsafe import clean_text

MAX_PROBLEMS = 10
_MORE = "... and more problems ignored"

LANG_RE = re.compile(r"^[A-Za-z0-9+#._-]{1,40}$")
TOPIC_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,49}$")
_KEYS = {"lang": "language", "language": "language", "stars": "stars", "days": "days",
         "host": "host", "sort": "sort", "topic": "topic"}
_FLAGS = {"nofork", "archived"}


@dataclass(frozen=True)
class ParsedQuery:
    text: str
    filters: SearchFilters
    problems: tuple[str, ...] = ()


def _show(value: str) -> str:
    """Make user text safe to echo: strip control/bidi characters, then truncate.

    Brackets are NOT stripped: callers must render problem messages as plain
    text (rich Text / autoescape), never as markup.
    """
    text = clean_text(value)
    return text if len(text) <= 40 else text[:40] + "..."


def _int(value: str, name: str, limit: int) -> int:
    if value.startswith(">="):
        value = value[2:]
    elif value.startswith(">"):
        value = value[1:]
    # ASCII digits only: int() would also accept unicode digits, "1_000" and "+5".
    digits = value.lstrip("0") or "0" if value.isascii() and value.isdigit() else ""
    if not digits or len(digits) > 12:
        raise ValueError(f"{name} needs a whole number, got '{_show(value)}'")
    n = int(digits)
    if not 0 <= n <= limit:
        raise ValueError(f"{name} must be between 0 and {limit}")
    return n


def _apply(filters: SearchFilters, key: str, value: str) -> SearchFilters:
    if key == "language":
        if not LANG_RE.match(value):
            raise ValueError(f"lang has unsupported characters: '{_show(value)}'")
        return replace(filters, language=value)
    if key == "stars":
        return replace(filters, min_stars=_int(value, "stars", MAX_STARS))
    if key == "days":
        return replace(filters, updated_within_days=_int(value, "days", MAX_DAYS) or None)
    if key == "host":
        v = value.lower()
        ids = registry().ids
        if v in ("both", "all"):
            return replace(filters, hosts=ids)
        if v in ids:
            return replace(filters, hosts=(v,))
        raise ValueError("host must be one of: " + ", ".join(ids + ("all",)))
    if key == "sort":
        v = value.lower()
        if v not in SORTS:
            raise ValueError("sort must be one of: " + ", ".join(SORTS))
        return replace(filters, sort=v)
    if key == "topic":
        v = value.lower()
        if not TOPIC_RE.match(v):
            raise ValueError(f"topic has unsupported characters: '{_show(value)}'")
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
            if len(problems) < MAX_PROBLEMS:
                problems.append(f"{key.lower()}: needs a value")
            elif len(problems) == MAX_PROBLEMS:
                problems.append(_MORE)
            continue
        try:
            filters = _apply(filters, name, value)
        except ValueError as e:
            if len(problems) < MAX_PROBLEMS:
                problems.append(str(e))
            elif len(problems) == MAX_PROBLEMS:
                problems.append(_MORE)
    return ParsedQuery(" ".join(words), filters, tuple(problems))
