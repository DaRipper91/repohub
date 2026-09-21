from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from repohub.core.hosts import registry

MAX_STARS = 10_000_000
MAX_DAYS = 36500

_ARM = re.compile(r"(aarch64|arm64|armv8)", re.I)
_X86 = re.compile(r"(x86[_-]64|amd64|x64)", re.I)


def parse_arch(name: str) -> str:
    if _ARM.search(name):
        return "arm64"
    if _X86.search(name):
        return "x86_64"
    return "unknown"


@dataclass(frozen=True)
class Asset:
    name: str
    size: int
    url: str
    arch: str


@dataclass(frozen=True)
class Release:
    tag: str
    published_at: str | None
    assets: tuple[Asset, ...] = ()

    @property
    def has_arm64(self) -> bool:
        return any(a.arch == "arm64" for a in self.assets)

    def to_dict(self) -> dict:
        return {"tag": self.tag, "published_at": self.published_at, "assets": [asdict(a) for a in self.assets]}

    @classmethod
    def from_dict(cls, d: dict) -> "Release":
        return cls(d["tag"], d["published_at"], tuple(Asset(**a) for a in d["assets"]))


@dataclass(frozen=True)
class Repo:
    host: str
    slug: str
    url: str
    description: str
    stars: int
    language: str
    license: str
    topics: tuple[str, ...]
    pushed_at: str
    archived: bool
    forks: int
    homepage: str
    fork: bool = False

    @property
    def key(self) -> str:
        return f"{self.host}:{self.slug.lower()}"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["topics"] = list(self.topics)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Repo":
        d = dict(d)
        d["topics"] = tuple(d.get("topics", ()))
        return cls(**d)


SORTS = ("stars", "updated", "forks")


@dataclass(frozen=True)
class SearchFilters:
    language: str | None = None
    min_stars: int = 0
    updated_within_days: int | None = None
    topic: str | None = None
    hosts: tuple[str, ...] = field(default_factory=lambda: registry().ids)
    include_archived: bool = False
    pushed_after: str | None = None  # YYYY-MM-DD, set by search_all from updated_within_days
    sort: str = "stars"
    hide_forks: bool = False
