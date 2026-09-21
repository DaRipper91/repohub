"""Shelves: search-based and curated, from packaged data and a user-edited YAML file.

Every value read from YAML is untrusted. Parsing uses ``yaml.safe_load`` only (no arbitrary
object construction); alias/anchor "billion laughs" expansion is bounded by the file-size
limit; all problems surface as ``ValueError`` naming the shelf and entry.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

import yaml
from platformdirs import user_config_dir

from repohub.core.models import SearchFilters
from repohub.core.providers.base import valid_slug
from repohub.core.textsafe import clean_text

HOSTS = ("github", "gitlab")
MAX_NOTE = 200
MAX_DESC = 300
MAX_STARS = 10_000_000
MAX_ENTRIES = 1000
MAX_SHELVES = 200
MAX_PERSONAL_BYTES = 1_000_000
MAX_PACKAGED_BYTES = 5_000_000
_NAME_LEN = 80


@dataclass(frozen=True)
class Snapshot:
    description: str = ""
    stars: int = 0
    language: str = ""
    license: str = ""
    pushed_at: str = ""


@dataclass(frozen=True)
class ShelfEntry:
    host: str
    slug: str
    note: str = ""
    snapshot: Snapshot | None = None

    @property
    def key(self) -> str:
        return f"{self.host}:{self.slug.lower()}"


@dataclass(frozen=True)
class Shelf:
    name: str
    query: str = ""
    topic: str | None = None
    language: str | None = None
    min_stars: int = 100
    days: int = 365
    repos: tuple[ShelfEntry, ...] = ()
    as_of: str | None = None

    @property
    def curated(self) -> bool:
        return bool(self.repos)

    def filters(self) -> SearchFilters:
        return SearchFilters(language=self.language, min_stars=self.min_stars,
                             updated_within_days=self.days, topic=self.topic)


@dataclass
class LoadedShelves:
    shelves: list[Shelf] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


_SHELF_KEYS = ("name", "query", "topic", "language", "min_stars", "days", "repos", "as_of")
_ENTRY_KEYS = ("repo", "note", "snapshot")
_SNAP_KEYS = ("description", "stars", "language", "license", "pushed_at")


def personal_shelves_path() -> Path:
    return Path(user_config_dir("repohub")) / "shelves.yaml"


def _show(value: object) -> str:
    """Bounded, sanitised repr of untrusted text for error messages."""
    return repr(clean_text(str(value))[:60])


def _is_int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _text(v: object, cap: int) -> str:
    return clean_text(v)[:cap]


def _parse_snapshot(raw: object) -> Snapshot | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("'snapshot' must be a mapping")
    unknown = [k for k in raw if k not in _SNAP_KEYS]
    if unknown:
        raise ValueError(f"snapshot: unknown key(s) {', '.join(_show(k) for k in unknown)}")
    vals: dict = {}
    for k, v in raw.items():
        if k == "stars":
            if not _is_int(v):
                raise ValueError("snapshot: 'stars' must be an integer")
            if not 0 <= v <= MAX_STARS:
                raise ValueError(f"snapshot: 'stars' must be between 0 and {MAX_STARS}")
            vals[k] = v
        else:
            if not isinstance(v, str):
                raise ValueError(f"snapshot: {k!r} must be a string")
            vals[k] = _text(v, MAX_DESC)
    return Snapshot(**vals)


def _parse_entry(raw: object) -> ShelfEntry:
    note: object = ""
    snap_raw: object = None
    if isinstance(raw, str):
        repo = raw
    elif isinstance(raw, dict):
        unknown = [k for k in raw if k not in _ENTRY_KEYS]
        if unknown:
            raise ValueError(f"unknown key(s) {', '.join(_show(k) for k in unknown)}")
        if "repo" not in raw:
            raise ValueError("missing 'repo'")
        repo = raw["repo"]
        if not isinstance(repo, str):
            raise ValueError("'repo' must be a string")
        note = raw.get("note") or ""
        if not isinstance(note, str):
            raise ValueError("'note' must be a string")
        snap_raw = raw.get("snapshot")
    else:
        raise ValueError(f"expected a string or mapping, got {type(raw).__name__}")
    host, sep, slug = repo.partition(":")
    if not sep:
        raise ValueError(f"{_show(repo)} must look like host:owner/name")
    host = host.strip().lower()
    if host not in HOSTS:
        raise ValueError(f"unknown host {_show(host)}")
    if not valid_slug(slug, host):
        raise ValueError(f"invalid slug {_show(slug)} for host {host}")
    return ShelfEntry(host, slug, _text(note, MAX_NOTE), _parse_snapshot(snap_raw))


def _parse_entries(name: str, raw: object) -> tuple[ShelfEntry, ...]:
    label = f"shelf {_show(name)}"
    if not isinstance(raw, list):
        raise ValueError(f"{label}: 'repos' must be a list")
    if len(raw) > MAX_ENTRIES:
        raise ValueError(f"{label}: too many entries ({len(raw)} > {MAX_ENTRIES})")
    seen: set[str] = set()
    out = []
    for n, item in enumerate(raw):
        try:
            entry = _parse_entry(item)
        except ValueError as e:
            raise ValueError(f"{label} entry #{n}: {e}") from e
        if entry.key in seen:
            raise ValueError(f"{label} entry #{n}: duplicate entry {_show(entry.key)}")
        seen.add(entry.key)
        out.append(entry)
    return tuple(out)


def _parse_shelf(i: int, item: object) -> Shelf:
    bad = f"invalid shelf #{i}"
    if not isinstance(item, dict):
        raise ValueError(f"{bad}: expected a mapping, got {type(item).__name__}")
    unknown = [k for k in item if k not in _SHELF_KEYS]
    if unknown:
        raise ValueError(f"{bad}: unknown key(s) {', '.join(_show(k) for k in unknown)}")
    if "name" not in item:
        raise ValueError(f"{bad}: missing required key 'name'")
    if not isinstance(item["name"], str) or not clean_text(item["name"]):
        raise ValueError(f"{bad}: name must be a non-empty string")
    name = _text(item["name"], _NAME_LEN)
    kw: dict = {"name": name}
    for k in ("query", "topic", "language", "as_of"):
        v = item.get(k)
        if v is None:
            continue
        if not isinstance(v, str):
            raise ValueError(f"{bad}: {k} must be a string")
        kw[k] = _text(v, MAX_DESC)
    for k in ("min_stars", "days"):
        if k in item:
            v = item[k]
            if not _is_int(v) or not 0 <= v <= MAX_STARS:
                raise ValueError(f"{bad}: {k} must be an integer between 0 and {MAX_STARS}")
            kw[k] = v
    if "repos" in item:
        kw["repos"] = _parse_entries(name, item["repos"])
    return Shelf(**kw)


def parse_shelves(data: object, source: str = "shelves") -> list[Shelf]:
    if data is None:
        return []
    if not isinstance(data, list):
        raise ValueError(f"{source} file must contain a list")
    if len(data) > MAX_SHELVES:
        raise ValueError(f"{source}: too many shelves ({len(data)} > {MAX_SHELVES})")
    return [_parse_shelf(i, item) for i, item in enumerate(data)]


def _safe_load(text: str) -> object:
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ValueError(f"invalid YAML: {clean_text(str(e))[:200]}") from e
    except RecursionError as e:
        raise ValueError("invalid YAML: nesting too deep") from e


def _read_file(path: Path, limit: int) -> str:
    with open(path, "rb") as fh:
        raw = fh.read(limit + 1)
    if len(raw) > limit:
        raise ValueError(f"file too large (over {limit} bytes)")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ValueError("file is not valid UTF-8") from e


def _packaged_text(name: str) -> str | None:
    res = resources.files("repohub.core").joinpath(name)
    if not res.is_file():
        return None
    raw = res.read_bytes()
    if len(raw) > MAX_PACKAGED_BYTES:
        raise ValueError(f"packaged {name} too large")
    return raw.decode("utf-8")


def load_shelves(path: Path | str | None = None) -> list[Shelf]:
    if path:
        text = _read_file(Path(path), MAX_PACKAGED_BYTES)
    else:
        text = _packaged_text("shelves.yaml") or ""
    return parse_shelves(_safe_load(text))


def load_all_shelves(personal_path: Path | str | None = None) -> LoadedShelves:
    result = LoadedShelves()
    for name in ("shelves.yaml", "catalog_shelves.yaml"):
        text = _packaged_text(name)
        if text is None:
            if name == "shelves.yaml":
                raise FileNotFoundError(name)
            continue  # the catalog is optional
        result.shelves.extend(parse_shelves(_safe_load(text), source=name))
    path = Path(personal_path) if personal_path else personal_shelves_path()
    try:
        if not path.exists():
            return result
        shelves = parse_shelves(_safe_load(_read_file(path, MAX_PERSONAL_BYTES)),
                                source="personal shelves")
    except Exception as e:  # a personal file must never break start-up
        result.problems.append(f"personal shelves ({path}): {clean_text(str(e))[:300]}")
        return result
    result.shelves.extend(shelves)
    return result
