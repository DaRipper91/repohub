# RepoHub Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build RepoHub, a store-style hub to search, browse and view GitHub and GitLab repositories, with favorites and safe cloning, as both a web app and a terminal app.

**Architecture:** One Python package. `repohub.core` holds providers (GitHub, GitLab), search merge, browse shelves, an SQLite cache, favorites, safe clone, and a `Hub` facade. `repohub.web` (FastAPI, Jinja, htmx) and `repohub.tui` (Textual) both call the `Hub` directly. Design: `docs/plans/2026-09-20-repohub-design.md`.

**Tech Stack:** Python 3.12, httpx, FastAPI, Jinja2, htmx (vendored), markdown-it-py, nh3, Textual, PyYAML, platformdirs, SQLite. Tests: pytest, pytest-asyncio, respx.

**Conventions**
- Work in `/home/daripper/Projects/repohub`. Run everything with the project venv: `.venv/bin/pytest`, `.venv/bin/python`.
- TDD: failing test, see it fail, minimal code, see it pass, commit.
- Every commit ends with this trailer (use two `-m` flags):
  `git commit -m "feat: ..." -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"`
- No test touches the network except tests marked `live`, which are excluded by default.
- Never log or print tokens.

**Deviations from the design doc (to record in Task 13):** a `core/hub.py` facade is added so both front ends share caching and stale fallback; shelves support a `topic` field (GitHub `topic:` qualifier, GitLab `topic=` param); `python-multipart` is a dependency for form posts; repo slugs are validated before any API call.

---

### Task 1: Project scaffold

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `src/repohub/__init__.py`, `src/repohub/core/__init__.py`, `src/repohub/core/providers/__init__.py`, `src/repohub/web/__init__.py`, `src/repohub/tui/__init__.py`, `tests/test_smoke.py`

**Step 1: Write the files**

`pyproject.toml`:
```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "repohub"
version = "0.1.0"
description = "A store-style hub for GitHub and GitLab repositories"
requires-python = ">=3.11"
dependencies = [
  "httpx>=0.27",
  "fastapi>=0.110",
  "uvicorn>=0.29",
  "jinja2>=3.1",
  "python-multipart>=0.0.9",
  "markdown-it-py>=3",
  "nh3>=0.2.17",
  "textual>=0.80",
  "pyyaml>=6",
  "platformdirs>=4",
]

[project.optional-dependencies]
dev = ["pytest>=8", "pytest-asyncio>=0.23", "respx>=0.21"]

[project.scripts]
repohub-web = "repohub.web.app:main"
repohub-tui = "repohub.tui.app:main"

[tool.hatch.build.targets.wheel]
packages = ["src/repohub"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
markers = ["live: hits the real GitHub/GitLab APIs (opt in with -m live)"]
addopts = "-m 'not live'"
```

`.gitignore`:
```
.venv/
__pycache__/
*.egg-info/
.pytest_cache/
*.db
```

`src/repohub/__init__.py`:
```python
__version__ = "0.1.0"
```
The other four `__init__.py` files are empty.

`tests/test_smoke.py`:
```python
import repohub


def test_version():
    assert repohub.__version__ == "0.1.0"
```

**Step 2: Create the venv and install**

Run: `uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -e ".[dev]"`
Expected: install completes without errors. (If a dependency has no aarch64 wheel and fails to build, stop and report it.)

**Step 3: Run tests**

Run: `.venv/bin/pytest -v`
Expected: `test_version PASSED`.

**Step 4: Commit**

```bash
git add pyproject.toml .gitignore src tests
git commit -m "chore: scaffold repohub package" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 2: Models and architecture parsing

**Files:**
- Create: `src/repohub/core/models.py`, `tests/test_models.py`

**Step 1: Write the failing tests** (`tests/test_models.py`)

```python
import pytest

from repohub.core.models import Asset, Release, Repo, SearchFilters, parse_arch


@pytest.mark.parametrize("name,arch", [
    ("tool-linux-aarch64.tar.gz", "arm64"),
    ("app-arm64-v8a.apk", "arm64"),
    ("tool_1.0_armv8.deb", "arm64"),
    ("tool_1.0_amd64.deb", "x86_64"),
    ("tool-x86_64-unknown-linux-gnu.tar.gz", "x86_64"),
    ("tool-win-x64.zip", "x86_64"),
    ("tool-source.zip", "unknown"),
])
def test_parse_arch(name, arch):
    assert parse_arch(name) == arch


def make_repo(**kw):
    base = dict(host="github", slug="Octo/Cat", url="https://github.com/Octo/Cat", description="d",
                stars=1, language="Go", license="MIT", topics=("a", "b"), pushed_at="2026-09-01T00:00:00Z",
                archived=False, forks=2, homepage="")
    base.update(kw)
    return Repo(**base)


def test_repo_key_is_lowercase_and_host_scoped():
    assert make_repo().key == "github:octo/cat"


def test_repo_roundtrip_through_dict():
    r = make_repo()
    assert Repo.from_dict(r.to_dict()) == r


def test_release_roundtrip_and_arm64_flag():
    rel = Release("v1", "2026-09-01T00:00:00Z", (Asset("a-arm64.tgz", 10, "https://x/a", "arm64"),))
    assert rel.has_arm64
    assert Release.from_dict(rel.to_dict()) == rel
    assert not Release("v1", None, ()).has_arm64


def test_filters_default_to_both_hosts_and_hide_archived():
    f = SearchFilters()
    assert f.hosts == ("github", "gitlab") and f.include_archived is False
```

**Step 2:** Run `.venv/bin/pytest tests/test_models.py -v`. Expected: FAIL (ImportError: cannot import name).

**Step 3: Implement** (`src/repohub/core/models.py`)

```python
from __future__ import annotations

import re
from dataclasses import asdict, dataclass

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


@dataclass(frozen=True)
class SearchFilters:
    language: str | None = None
    min_stars: int = 0
    updated_within_days: int | None = None
    topic: str | None = None
    hosts: tuple[str, ...] = ("github", "gitlab")
    include_archived: bool = False
    pushed_after: str | None = None  # YYYY-MM-DD, set by search_all from updated_within_days
```

**Step 4:** Run `.venv/bin/pytest tests/test_models.py -v`. Expected: all PASS.

**Step 5: Commit**

```bash
git add src/repohub/core/models.py tests/test_models.py
git commit -m "feat: add normalized models and arch parsing" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 3: Token discovery

**Files:**
- Create: `src/repohub/core/auth.py`, `tests/test_auth.py`

**Step 1: Failing tests**

```python
from repohub.core.auth import Tokens, find_tokens


def no_cli():
    return None


def test_env_tokens_win_over_gh_cli():
    t = find_tokens({"GITHUB_TOKEN": "envgh", "GITLAB_TOKEN": "envgl"}, gh_cli=lambda: "clitoken")
    assert (t.github, t.gitlab) == ("envgh", "envgl")


def test_gh_token_alias_is_accepted():
    assert find_tokens({"GH_TOKEN": "x"}, gh_cli=no_cli).github == "x"


def test_falls_back_to_gh_cli():
    assert find_tokens({}, gh_cli=lambda: "clitoken").github == "clitoken"


def test_anonymous_when_nothing_found():
    t = find_tokens({}, gh_cli=no_cli)
    assert t.github is None and t.gitlab is None


def test_empty_values_count_as_missing():
    assert find_tokens({"GITHUB_TOKEN": ""}, gh_cli=no_cli).github is None


def test_repr_never_contains_tokens():
    t = Tokens(github="secret-gh", gitlab="secret-gl")
    assert "secret" not in repr(t) and "secret" not in str(t)
```

**Step 2:** Run `.venv/bin/pytest tests/test_auth.py -v`. Expected: FAIL (ImportError).

**Step 3: Implement**

```python
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from typing import Callable, Mapping


@dataclass(frozen=True)
class Tokens:
    github: str | None = field(default=None, repr=False)
    gitlab: str | None = field(default=None, repr=False)


def _gh_cli_token() -> str | None:
    try:
        r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    token = r.stdout.strip()
    return token if r.returncode == 0 and token else None


def find_tokens(env: Mapping[str, str] | None = None, gh_cli: Callable[[], str | None] = _gh_cli_token) -> Tokens:
    env = os.environ if env is None else env
    github = env.get("GITHUB_TOKEN") or env.get("GH_TOKEN") or gh_cli()
    gitlab = env.get("GITLAB_TOKEN")
    return Tokens(github or None, gitlab or None)
```

**Step 4:** Run `.venv/bin/pytest tests/test_auth.py -v`. Expected: PASS.

**Step 5: Commit**

```bash
git add src/repohub/core/auth.py tests/test_auth.py
git commit -m "feat: discover API tokens from env or gh CLI" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 4: SQLite cache with TTL

**Files:**
- Create: `src/repohub/core/cache.py`, `tests/test_cache.py`

**Step 1: Failing tests**

```python
from repohub.core.cache import Cache


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_set_and_get_roundtrip():
    c = Cache(now=Clock())
    c.set("k", {"a": [1, 2]}, ttl=60)
    assert c.get("k") == {"a": [1, 2]}


def test_missing_key_is_none():
    assert Cache(now=Clock()).get("nope") is None


def test_expired_entries_are_hidden_unless_stale_allowed():
    clock = Clock()
    c = Cache(now=clock)
    c.set("k", "v", ttl=10)
    clock.t += 11
    assert c.get("k") is None
    assert c.get("k", allow_stale=True) == "v"


def test_overwrite_replaces_value_and_ttl():
    clock = Clock()
    c = Cache(now=clock)
    c.set("k", "old", ttl=1)
    c.set("k", "new", ttl=100)
    clock.t += 50
    assert c.get("k") == "new"


def test_persists_to_file(tmp_path):
    p = tmp_path / "c.db"
    Cache(str(p)).set("k", 1, ttl=100)
    assert Cache(str(p)).get("k") == 1
```

**Step 2:** Run `.venv/bin/pytest tests/test_cache.py -v`. Expected: FAIL (ImportError).

**Step 3: Implement**

```python
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any, Callable


class Cache:
    def __init__(self, path: str = ":memory:", now: Callable[[], float] = time.time):
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self._lock = threading.Lock()
        self._now = now
        with self._lock:
            self._db.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT NOT NULL, expires REAL NOT NULL)")
            self._db.commit()

    def get(self, key: str, allow_stale: bool = False) -> Any | None:
        with self._lock:
            row = self._db.execute("SELECT value, expires FROM cache WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        if row[1] < self._now() and not allow_stale:
            return None
        return json.loads(row[0])

    def set(self, key: str, value: Any, ttl: float) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO cache (key, value, expires) VALUES (?, ?, ?)",
                (key, json.dumps(value), self._now() + ttl),
            )
            self._db.commit()
```

**Step 4:** Run `.venv/bin/pytest tests/test_cache.py -v`. Expected: PASS.

**Step 5: Commit**

```bash
git add src/repohub/core/cache.py tests/test_cache.py
git commit -m "feat: add SQLite response cache with TTL" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 5: Provider base and GitHub provider

**Files:**
- Create: `src/repohub/core/providers/base.py`, `src/repohub/core/providers/github.py`, `tests/test_github.py`

**Step 1: Failing tests** (`tests/test_github.py`)

```python
import httpx
import pytest
import respx

from repohub.core.models import SearchFilters
from repohub.core.providers.base import ProviderError, RateLimited
from repohub.core.providers.github import GitHubProvider

API = "https://api.github.com"
ITEM = {
    "full_name": "o/r", "html_url": "https://github.com/o/r", "description": "d", "stargazers_count": 5,
    "language": "Rust", "license": {"spdx_id": "MIT"}, "topics": ["tui"], "pushed_at": "2026-09-01T00:00:00Z",
    "archived": False, "forks_count": 1, "homepage": None,
}


@respx.mock
async def test_search_builds_query_and_maps_repo():
    route = respx.get(f"{API}/search/repositories").mock(return_value=httpx.Response(200, json={"items": [ITEM]}))
    repos = await GitHubProvider().search(
        "tui", SearchFilters(language="Rust", min_stars=10, topic="cli", pushed_after="2026-01-01"))
    q = route.calls.last.request.url.params["q"]
    for part in ("tui", "stars:>=10", 'language:"Rust"', "topic:cli", "pushed:>=2026-01-01", "archived:false"):
        assert part in q
    r = repos[0]
    assert (r.host, r.slug, r.license, r.homepage, r.topics) == ("github", "o/r", "MIT", "", ("tui",))


@respx.mock
async def test_archived_qualifier_omitted_when_included():
    route = respx.get(f"{API}/search/repositories").mock(return_value=httpx.Response(200, json={"items": []}))
    await GitHubProvider().search("x", SearchFilters(include_archived=True))
    assert "archived:false" not in route.calls.last.request.url.params["q"]


@respx.mock
async def test_token_sent_only_when_present():
    route = respx.get(f"{API}/search/repositories").mock(return_value=httpx.Response(200, json={"items": []}))
    await GitHubProvider(token="tok").search("x", SearchFilters())
    assert route.calls.last.request.headers["authorization"] == "Bearer tok"
    await GitHubProvider().search("x", SearchFilters())
    assert "authorization" not in route.calls.last.request.headers


@respx.mock
async def test_rate_limit_raises_with_reset_time():
    respx.get(f"{API}/search/repositories").mock(return_value=httpx.Response(
        403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1790000000"}, json={}))
    with pytest.raises(RateLimited) as e:
        await GitHubProvider().search("x", SearchFilters())
    assert e.value.reset_at == 1790000000


@respx.mock
async def test_bad_token_and_network_errors_are_provider_errors():
    respx.get(f"{API}/search/repositories").mock(return_value=httpx.Response(401, json={}))
    with pytest.raises(ProviderError, match="token rejected"):
        await GitHubProvider(token="bad").search("x", SearchFilters())
    respx.get(f"{API}/search/repositories").mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(ProviderError, match="network"):
        await GitHubProvider().search("x", SearchFilters())


@respx.mock
async def test_repo_readme_and_release():
    respx.get(f"{API}/repos/o/r").mock(return_value=httpx.Response(200, json=ITEM))
    respx.get(f"{API}/repos/o/r/readme").mock(return_value=httpx.Response(200, text="# Hi"))
    respx.get(f"{API}/repos/o/r/releases/latest").mock(return_value=httpx.Response(200, json={
        "tag_name": "v1", "published_at": "2026-09-02T00:00:00Z",
        "assets": [{"name": "t-aarch64.tgz", "size": 9, "browser_download_url": "https://x/t"}]}))
    p = GitHubProvider()
    assert (await p.repo("o/r")).stars == 5
    assert await p.readme("o/r") == "# Hi"
    rel = await p.latest_release("o/r")
    assert rel.tag == "v1" and rel.has_arm64


@respx.mock
async def test_missing_readme_and_release_are_none():
    respx.get(f"{API}/repos/o/r/readme").mock(return_value=httpx.Response(404))
    respx.get(f"{API}/repos/o/r/releases/latest").mock(return_value=httpx.Response(404))
    p = GitHubProvider()
    assert await p.readme("o/r") is None
    assert await p.latest_release("o/r") is None


@respx.mock
async def test_invalid_slug_never_reaches_network():
    p = GitHubProvider()
    for bad in ("../etc/passwd", "o/r/extra", "o", "o/../x", "o/r?x=1"):
        with pytest.raises(ProviderError, match="invalid"):
            await p.repo(bad)
    assert respx.calls.call_count == 0
```

**Step 2:** Run `.venv/bin/pytest tests/test_github.py -v`. Expected: FAIL (ImportError).

**Step 3: Implement**

`src/repohub/core/providers/base.py`:
```python
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
    if not _SLUG.match(slug):
        return False
    parts = slug.split("/")
    if any(p in (".", "..") for p in parts):
        return False
    return len(parts) == 2 if host == "github" else True
```

`src/repohub/core/providers/github.py`:
```python
from __future__ import annotations

import httpx

from repohub.core.models import Asset, Release, Repo, SearchFilters, parse_arch
from repohub.core.providers.base import ProviderError, RateLimited, valid_slug


def _to_repo(item: dict) -> Repo:
    lic = (item.get("license") or {}).get("spdx_id") or ""
    if lic == "NOASSERTION":
        lic = "other"
    return Repo(
        host="github", slug=item["full_name"], url=item["html_url"], description=item.get("description") or "",
        stars=item["stargazers_count"], language=item.get("language") or "", license=lic,
        topics=tuple(item.get("topics") or ()), pushed_at=item.get("pushed_at") or "",
        archived=bool(item.get("archived")), forks=item.get("forks_count", 0), homepage=item.get("homepage") or "",
    )


class GitHubProvider:
    host = "github"

    def __init__(self, token: str | None = None, base_url: str = "https://api.github.com"):
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "repohub"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.AsyncClient(base_url=base_url, headers=headers, timeout=15)

    async def aclose(self) -> None:
        await self._client.aclose()

    def _check_slug(self, slug: str) -> None:
        if not valid_slug(slug, "github"):
            raise ProviderError(self.host, "invalid repository name")

    async def _get(self, path: str, params: dict | None = None, accept: str | None = None) -> httpx.Response | None:
        try:
            resp = await self._client.get(path, params=params, headers={"Accept": accept} if accept else None)
        except httpx.HTTPError as e:
            raise ProviderError(self.host, "network error") from e
        limited = resp.headers.get("x-ratelimit-remaining") == "0"
        if resp.status_code == 429 or (resp.status_code == 403 and limited):
            reset = resp.headers.get("x-ratelimit-reset", "")
            raise RateLimited(self.host, "rate limited", int(reset) if reset.isdigit() else None)
        if resp.status_code == 401:
            raise ProviderError(self.host, "token rejected")
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise ProviderError(self.host, f"HTTP {resp.status_code}")
        return resp

    async def search(self, query: str, filters: SearchFilters, per_page: int = 30) -> list[Repo]:
        parts = [query.strip()] if query.strip() else []
        if filters.min_stars:
            parts.append(f"stars:>={filters.min_stars}")
        if filters.language:
            parts.append('language:"%s"' % filters.language.replace('"', ""))
        if filters.topic:
            parts.append(f"topic:{filters.topic}")
        if filters.pushed_after:
            parts.append(f"pushed:>={filters.pushed_after}")
        if not filters.include_archived:
            parts.append("archived:false")
        resp = await self._get("/search/repositories",
                               {"q": " ".join(parts), "sort": "stars", "order": "desc", "per_page": per_page})
        return [_to_repo(i) for i in resp.json()["items"]]

    async def repo(self, slug: str) -> Repo:
        self._check_slug(slug)
        resp = await self._get(f"/repos/{slug}")
        if resp is None:
            raise ProviderError(self.host, "repository not found")
        return _to_repo(resp.json())

    async def readme(self, slug: str) -> str | None:
        self._check_slug(slug)
        resp = await self._get(f"/repos/{slug}/readme", accept="application/vnd.github.raw+json")
        return resp.text if resp is not None else None

    async def latest_release(self, slug: str) -> Release | None:
        self._check_slug(slug)
        resp = await self._get(f"/repos/{slug}/releases/latest")
        if resp is None:
            return None
        j = resp.json()
        assets = tuple(Asset(a["name"], a.get("size", 0), a["browser_download_url"], parse_arch(a["name"]))
                       for a in j.get("assets", []))
        return Release(j["tag_name"], j.get("published_at"), assets)
```

**Step 4:** Run `.venv/bin/pytest tests/test_github.py -v`. Expected: PASS.

**Step 5: Commit**

```bash
git add src/repohub/core/providers tests/test_github.py
git commit -m "feat: add GitHub provider with rate limit and slug validation" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 6: GitLab provider

**Files:**
- Create: `src/repohub/core/providers/gitlab.py`, `tests/test_gitlab.py`

**Step 1: Failing tests**

```python
import httpx
import pytest
import respx

from repohub.core.models import SearchFilters
from repohub.core.providers.base import ProviderError, RateLimited
from repohub.core.providers.gitlab import GitLabProvider

API = "https://gitlab.com/api/v4"
ITEM = {
    "id": 7, "path_with_namespace": "g/sub/p", "web_url": "https://gitlab.com/g/sub/p", "description": None,
    "star_count": 42, "topics": ["cli"], "last_activity_at": "2026-09-03T00:00:00Z", "archived": False,
    "forks_count": 3, "license": {"name": "MIT License"},
}


@respx.mock
async def test_search_params_and_mapping():
    route = respx.get(f"{API}/projects").mock(return_value=httpx.Response(200, json=[ITEM]))
    repos = await GitLabProvider().search(
        "cli", SearchFilters(language="Rust", topic="cli", pushed_after="2026-01-01"))
    p = route.calls.last.request.url.params
    assert p["search"] == "cli" and p["order_by"] == "star_count" and p["archived"] == "false"
    assert p["with_programming_language"] == "Rust" and p["topic"] == "cli"
    assert p["last_activity_after"].startswith("2026-01-01")
    r = repos[0]
    assert (r.host, r.slug, r.stars, r.description, r.license) == ("gitlab", "g/sub/p", 42, "", "MIT License")
    assert r.language == "Rust"


@respx.mock
async def test_private_token_header():
    route = respx.get(f"{API}/projects").mock(return_value=httpx.Response(200, json=[]))
    await GitLabProvider(token="tok").search("x", SearchFilters())
    assert route.calls.last.request.headers["private-token"] == "tok"


@respx.mock
async def test_rate_limit_and_errors():
    respx.get(f"{API}/projects").mock(return_value=httpx.Response(429, headers={"retry-after": "30"}))
    with pytest.raises(RateLimited):
        await GitLabProvider().search("x", SearchFilters())
    respx.get(f"{API}/projects").mock(return_value=httpx.Response(401))
    with pytest.raises(ProviderError, match="token rejected"):
        await GitLabProvider(token="bad").search("x", SearchFilters())
    respx.get(f"{API}/projects").mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(ProviderError, match="network"):
        await GitLabProvider().search("x", SearchFilters())


@respx.mock
async def test_repo_uses_top_language_and_encoded_slug():
    route = respx.get(f"{API}/projects/g%2Fsub%2Fp").mock(return_value=httpx.Response(200, json=ITEM))
    respx.get(f"{API}/projects/7/languages").mock(return_value=httpx.Response(200, json={"Go": 20.0, "Rust": 80.0}))
    r = await GitLabProvider().repo("g/sub/p")
    assert r.language == "Rust" and route.called


@respx.mock
async def test_readme_tries_candidates_until_found():
    respx.get(f"{API}/projects/g%2Fp/repository/files/README.md/raw").mock(return_value=httpx.Response(404))
    respx.get(f"{API}/projects/g%2Fp/repository/files/README.rst/raw").mock(return_value=httpx.Response(200, text="rst"))
    assert await GitLabProvider().readme("g/p") == "rst"


@respx.mock
async def test_latest_release_maps_link_assets():
    respx.get(f"{API}/projects/g%2Fp/releases").mock(return_value=httpx.Response(200, json=[{
        "tag_name": "v2", "released_at": "2026-09-04T00:00:00Z",
        "assets": {"links": [{"name": "t-arm64.zip", "url": "https://x/t", "direct_asset_url": "https://x/d"}]}}]))
    rel = await GitLabProvider().latest_release("g/p")
    assert rel.tag == "v2" and rel.has_arm64 and rel.assets[0].url == "https://x/d"


@respx.mock
async def test_no_releases_is_none_and_bad_slug_rejected():
    respx.get(f"{API}/projects/g%2Fp/releases").mock(return_value=httpx.Response(200, json=[]))
    assert await GitLabProvider().latest_release("g/p") is None
    with pytest.raises(ProviderError, match="invalid"):
        await GitLabProvider().repo("g/../x")
```

**Step 2:** Run `.venv/bin/pytest tests/test_gitlab.py -v`. Expected: FAIL (ImportError).

**Step 3: Implement** (`src/repohub/core/providers/gitlab.py`)

```python
from __future__ import annotations

import time
from urllib.parse import quote

import httpx

from repohub.core.models import Asset, Release, Repo, SearchFilters, parse_arch
from repohub.core.providers.base import ProviderError, RateLimited, valid_slug

README_NAMES = ("README.md", "README.markdown", "README.rst", "README.txt", "README")


def _to_repo(item: dict, language: str = "") -> Repo:
    return Repo(
        host="gitlab", slug=item["path_with_namespace"], url=item["web_url"], description=item.get("description") or "",
        stars=item.get("star_count", 0), language=language, license=(item.get("license") or {}).get("name") or "",
        topics=tuple(item.get("topics") or item.get("tag_list") or ()), pushed_at=item.get("last_activity_at") or "",
        archived=bool(item.get("archived")), forks=item.get("forks_count", 0), homepage="",
    )


class GitLabProvider:
    host = "gitlab"

    def __init__(self, token: str | None = None, base_url: str = "https://gitlab.com/api/v4"):
        headers = {"User-Agent": "repohub"}
        if token:
            headers["PRIVATE-TOKEN"] = token
        self._client = httpx.AsyncClient(base_url=base_url, headers=headers, timeout=15)

    async def aclose(self) -> None:
        await self._client.aclose()

    def _check_slug(self, slug: str) -> str:
        if not valid_slug(slug, "gitlab"):
            raise ProviderError(self.host, "invalid repository name")
        return quote(slug, safe="")

    async def _get(self, path: str, params: dict | None = None) -> httpx.Response | None:
        try:
            resp = await self._client.get(path, params=params)
        except httpx.HTTPError as e:
            raise ProviderError(self.host, "network error") from e
        if resp.status_code == 429:
            retry = resp.headers.get("retry-after", "")
            raise RateLimited(self.host, "rate limited", int(time.time()) + int(retry) if retry.isdigit() else None)
        if resp.status_code == 401:
            raise ProviderError(self.host, "token rejected")
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise ProviderError(self.host, f"HTTP {resp.status_code}")
        return resp

    async def search(self, query: str, filters: SearchFilters, per_page: int = 30) -> list[Repo]:
        params: dict = {"order_by": "star_count", "sort": "desc", "per_page": per_page, "license": "true"}
        if query.strip():
            params["search"] = query.strip()
        if not filters.include_archived:
            params["archived"] = "false"
        if filters.language:
            params["with_programming_language"] = filters.language
        if filters.topic:
            params["topic"] = filters.topic
        if filters.pushed_after:
            params["last_activity_after"] = f"{filters.pushed_after}T00:00:00Z"
        resp = await self._get("/projects", params)
        return [_to_repo(i, filters.language or "") for i in resp.json()]

    async def repo(self, slug: str) -> Repo:
        enc = self._check_slug(slug)
        resp = await self._get(f"/projects/{enc}", {"license": "true"})
        if resp is None:
            raise ProviderError(self.host, "repository not found")
        item = resp.json()
        language = ""
        try:
            langs = await self._get(f"/projects/{item['id']}/languages")
            if langs is not None and langs.json():
                data = langs.json()
                language = max(data, key=data.get)
        except ProviderError:
            pass
        return _to_repo(item, language)

    async def readme(self, slug: str) -> str | None:
        enc = self._check_slug(slug)
        for name in README_NAMES:
            resp = await self._get(f"/projects/{enc}/repository/files/{quote(name, safe='')}/raw", {"ref": "HEAD"})
            if resp is not None:
                return resp.text
        return None

    async def latest_release(self, slug: str) -> Release | None:
        enc = self._check_slug(slug)
        resp = await self._get(f"/projects/{enc}/releases", {"per_page": 1})
        items = resp.json() if resp is not None else []
        if not items:
            return None
        j = items[0]
        assets = tuple(
            Asset(a["name"], 0, a.get("direct_asset_url") or a["url"], parse_arch(a["name"]))
            for a in (j.get("assets") or {}).get("links", [])
        )
        return Release(j["tag_name"], j.get("released_at"), assets)
```

**Step 4:** Run `.venv/bin/pytest tests/test_gitlab.py -v`. Expected: PASS.

**Step 5: Commit**

```bash
git add src/repohub/core/providers/gitlab.py tests/test_gitlab.py
git commit -m "feat: add GitLab provider" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 7: Cross-host search

**Files:**
- Create: `src/repohub/core/search.py`, `tests/helpers.py`, `tests/test_search.py`

**Step 1: Test helpers** (`tests/helpers.py`)

```python
from repohub.core.models import Repo


def mk(host="github", slug="o/r", stars=1, **kw):
    base = dict(host=host, slug=slug, url=f"https://{host}.com/{slug}", description="d", stars=stars,
                language="Go", license="MIT", topics=(), pushed_at="2026-09-10T00:00:00Z",
                archived=False, forks=0, homepage="")
    base.update(kw)
    return Repo(**base)


class FakeProvider:
    def __init__(self, host, repos=None, error=None):
        self.host, self.repos, self.error, self.calls = host, repos or [], error, 0

    async def search(self, query, filters, per_page=30):
        self.calls += 1
        if self.error:
            raise self.error
        return list(self.repos)
```

**Step 2: Failing tests** (`tests/test_search.py`)

```python
from datetime import datetime, timezone

from helpers import FakeProvider, mk
from repohub.core.models import SearchFilters
from repohub.core.providers.base import ProviderError
from repohub.core.search import SearchResult, search_all


async def run(providers, filters=SearchFilters(), now=None):
    return await search_all({p.host: p for p in providers}, "q", filters, now=now)


async def test_merges_and_sorts_by_stars():
    gh = FakeProvider("github", [mk("github", "a/a", 5), mk("github", "b/b", 50)])
    gl = FakeProvider("gitlab", [mk("gitlab", "c/c", 20)])
    r = await run([gh, gl])
    assert [x.slug for x in r.repos] == ["b/b", "c/c", "a/a"] and r.errors == {}


async def test_dedupes_same_slug_keeping_higher_stars_and_preferring_github_on_tie():
    gh = FakeProvider("github", [mk("github", "x/y", 10), mk("github", "t/t", 7)])
    gl = FakeProvider("gitlab", [mk("gitlab", "X/Y", 10), mk("gitlab", "t/t", 9)])
    r = await run([gh, gl])
    by = {x.slug.lower(): x for x in r.repos}
    assert by["x/y"].host == "github" and by["t/t"].host == "gitlab" and len(r.repos) == 2


async def test_one_host_failing_keeps_other_results_and_reports_error():
    gh = FakeProvider("github", error=ProviderError("github", "rate limited"))
    gl = FakeProvider("gitlab", [mk("gitlab", "c/c", 3)])
    r = await run([gh, gl])
    assert [x.slug for x in r.repos] == ["c/c"] and r.errors == {"github": "rate limited"}


async def test_unexpected_exception_does_not_leak_details():
    gh = FakeProvider("github", error=RuntimeError("secret token abc"))
    r = await run([gh])
    assert r.errors == {"github": "unexpected error"}


async def test_client_side_filters():
    gl = FakeProvider("gitlab", [
        mk("gitlab", "a/a", 5), mk("gitlab", "b/b", 500), mk("gitlab", "c/c", 900, archived=True),
        mk("gitlab", "d/d", 800, pushed_at="2020-01-01T00:00:00Z"),
    ])
    now = datetime(2026, 9, 20, tzinfo=timezone.utc)
    r = await run([gl], SearchFilters(min_stars=100, updated_within_days=30), now=now)
    assert [x.slug for x in r.repos] == ["b/b"]
    r2 = await run([gl], SearchFilters(min_stars=100, include_archived=True), now=now)
    assert "c/c" in [x.slug for x in r2.repos]


async def test_only_selected_hosts_are_queried():
    gh, gl = FakeProvider("github", [mk()]), FakeProvider("gitlab", [mk("gitlab", "z/z")])
    r = await run([gh, gl], SearchFilters(hosts=("gitlab",)))
    assert gh.calls == 0 and gl.calls == 1 and [x.host for x in r.repos] == ["gitlab"]


def test_result_roundtrip():
    r = SearchResult([mk()], {"gitlab": "x"}, stale=True)
    assert SearchResult.from_dict(r.to_dict()) == r
```

Add `pythonpath = ["tests"]` to `[tool.pytest.ini_options]` in `pyproject.toml` so `from helpers import ...` works.

**Step 3:** Run `.venv/bin/pytest tests/test_search.py -v`. Expected: FAIL (ImportError).

**Step 4: Implement** (`src/repohub/core/search.py`)

```python
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone

from repohub.core.models import Repo, SearchFilters
from repohub.core.providers.base import ProviderError


@dataclass
class SearchResult:
    repos: list[Repo] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    stale: bool = False

    def to_dict(self) -> dict:
        return {"repos": [r.to_dict() for r in self.repos], "errors": self.errors, "stale": self.stale}

    @classmethod
    def from_dict(cls, d: dict) -> "SearchResult":
        return cls([Repo.from_dict(r) for r in d["repos"]], dict(d["errors"]), bool(d.get("stale")))


def _keep(repo: Repo, f: SearchFilters) -> bool:
    if repo.stars < f.min_stars:
        return False
    if repo.archived and not f.include_archived:
        return False
    if f.pushed_after and repo.pushed_at[:10] < f.pushed_after:
        return False
    return True


async def search_all(providers: dict, query: str, filters: SearchFilters, now: datetime | None = None) -> SearchResult:
    if filters.updated_within_days:
        now = now or datetime.now(timezone.utc)
        cutoff = (now - timedelta(days=filters.updated_within_days)).date().isoformat()
        filters = replace(filters, pushed_after=cutoff)
    hosts = [h for h in filters.hosts if h in providers]
    outcomes = await asyncio.gather(*(providers[h].search(query, filters) for h in hosts), return_exceptions=True)
    errors: dict[str, str] = {}
    best: dict[str, Repo] = {}
    for host, out in zip(hosts, outcomes):
        if isinstance(out, ProviderError):
            errors[host] = str(out)
            continue
        if isinstance(out, BaseException):
            errors[host] = "unexpected error"
            continue
        for repo in out:
            if not _keep(repo, filters):
                continue
            cur = best.get(repo.slug.lower())
            if cur is None or repo.stars > cur.stars or (repo.stars == cur.stars and repo.host == "github"):
                best[repo.slug.lower()] = repo
    repos = sorted(best.values(), key=lambda r: (-r.stars, r.host != "github", r.slug.lower()))
    return SearchResult(repos, errors)
```

**Step 5:** Run `.venv/bin/pytest -v`. Expected: all PASS.

**Step 6: Commit**

```bash
git add src/repohub/core/search.py tests pyproject.toml
git commit -m "feat: add cross-host search with merge, dedupe and partial failure" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 8: Favorites store

**Files:**
- Create: `src/repohub/core/store.py`, `tests/test_store.py`

**Step 1: Failing tests**

```python
from helpers import mk
from repohub.core.store import Favorites


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


def test_add_list_remove():
    f = Favorites()
    r = mk("github", "O/R", 5)
    f.add(r)
    assert f.is_favorite(r.key) and f.list() == [r]
    f.remove(r.key)
    assert not f.is_favorite(r.key) and f.list() == []


def test_add_twice_keeps_one_row():
    f = Favorites()
    f.add(mk())
    f.add(mk(stars=99))
    assert len(f.list()) == 1 and f.list()[0].stars == 99


def test_stale_lists_entries_not_refreshed_recently():
    clock = Clock()
    f = Favorites(now=clock)
    f.add(mk("github", "a/a"))
    clock.t += 100
    assert f.stale(max_age=50) == [mk("github", "a/a")]
    assert f.stale(max_age=500) == []


def test_update_marks_fresh_and_persists(tmp_path):
    clock = Clock()
    p = str(tmp_path / "f.db")
    f = Favorites(p, now=clock)
    f.add(mk("github", "a/a", 1))
    clock.t += 100
    f.update(mk("github", "a/a", 7))
    assert f.stale(max_age=50) == []
    assert Favorites(p).list()[0].stars == 7
```

**Step 2:** Run `.venv/bin/pytest tests/test_store.py -v`. Expected: FAIL.

**Step 3: Implement**

```python
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Callable

from repohub.core.models import Repo


class Favorites:
    def __init__(self, path: str = ":memory:", now: Callable[[], float] = time.time):
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self._lock = threading.Lock()
        self._now = now
        with self._lock:
            self._db.execute("CREATE TABLE IF NOT EXISTS favorites (key TEXT PRIMARY KEY, data TEXT NOT NULL, "
                             "added_at REAL NOT NULL, refreshed_at REAL NOT NULL)")
            self._db.commit()

    def add(self, repo: Repo) -> None:
        t = self._now()
        with self._lock:
            self._db.execute(
                "INSERT INTO favorites (key, data, added_at, refreshed_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET data = excluded.data, refreshed_at = excluded.refreshed_at",
                (repo.key, json.dumps(repo.to_dict()), t, t))
            self._db.commit()

    update = add

    def remove(self, key: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM favorites WHERE key = ?", (key,))
            self._db.commit()

    def is_favorite(self, key: str) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM favorites WHERE key = ?", (key,)).fetchone() is not None

    def list(self) -> list[Repo]:
        with self._lock:
            rows = self._db.execute("SELECT data FROM favorites ORDER BY added_at DESC, key").fetchall()
        return [Repo.from_dict(json.loads(r[0])) for r in rows]

    def stale(self, max_age: float) -> list[Repo]:
        cutoff = self._now() - max_age
        with self._lock:
            rows = self._db.execute("SELECT data FROM favorites WHERE refreshed_at < ?", (cutoff,)).fetchall()
        return [Repo.from_dict(json.loads(r[0])) for r in rows]
```

**Step 4:** Run `.venv/bin/pytest tests/test_store.py -v`. Expected: PASS.

**Step 5: Commit**

```bash
git add src/repohub/core/store.py tests/test_store.py
git commit -m "feat: add favorites store" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 9: Safe clone

**Files:**
- Create: `src/repohub/core/clone.py`, `tests/test_clone.py`

**Step 1: Failing tests**

```python
import subprocess

import pytest

from repohub.core.clone import CloneError, clone, clone_url, plan_clone


def test_clone_url_builds_from_host_and_slug():
    assert clone_url("github", "o/r") == "https://github.com/o/r.git"
    assert clone_url("gitlab", "g/sub/p") == "https://gitlab.com/g/sub/p.git"


@pytest.mark.parametrize("host,slug", [("bitbucket", "o/r"), ("github", "../x"), ("github", "o")])
def test_clone_url_rejects_bad_input(host, slug):
    with pytest.raises(CloneError):
        clone_url(host, slug)


@pytest.mark.parametrize("url", [
    "http://github.com/a/b", "https://evil.com/a/b", "https://github.com.evil.com/a/b",
    "https://user@github.com/a/b", "https://github.com:444/a/b", "https://github.com/a",
    "https://github.com/a/../b", "git@github.com:a/b", "https://github.com/a/-rf", "file:///etc/passwd",
])
def test_plan_rejects_unsafe_urls(url, tmp_path):
    with pytest.raises(CloneError):
        plan_clone(url, tmp_path)


def test_plan_returns_target_inside_root(tmp_path):
    assert plan_clone("https://github.com/a/b.git", tmp_path) == tmp_path.resolve() / "b"


def test_plan_refuses_existing_folder(tmp_path):
    (tmp_path / "b").mkdir()
    with pytest.raises(CloneError, match="exists"):
        plan_clone("https://github.com/a/b.git", tmp_path)


def test_clone_runs_git_without_shell_and_shallow_by_default(tmp_path):
    seen = {}

    def runner(args, **kw):
        seen["args"], seen["kw"] = args, kw
        return subprocess.CompletedProcess(args, 0, "", "")

    target = clone("https://github.com/a/b.git", tmp_path, runner=runner)
    assert target == tmp_path.resolve() / "b"
    assert seen["args"] == ["git", "clone", "--depth", "1", "--", "https://github.com/a/b.git", str(target)]
    assert not seen["kw"].get("shell")
    assert seen["kw"]["env"]["GIT_TERMINAL_PROMPT"] == "0"


def test_clone_failure_becomes_clone_error(tmp_path):
    def runner(args, **kw):
        return subprocess.CompletedProcess(args, 128, "", "fatal: repository not found")

    with pytest.raises(CloneError, match="not found"):
        clone("https://github.com/a/b.git", tmp_path, runner=runner)
```

**Step 2:** Run `.venv/bin/pytest tests/test_clone.py -v`. Expected: FAIL.

**Step 3: Implement**

```python
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from repohub.core.providers.base import valid_slug

HOSTS = {"github": "github.com", "gitlab": "gitlab.com"}
_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")


class CloneError(Exception):
    pass


def clone_url(host: str, slug: str) -> str:
    if host not in HOSTS or not valid_slug(slug, host):
        raise CloneError("invalid repository")
    return f"https://{HOSTS[host]}/{slug}.git"


def plan_clone(url: str, dest_root: Path | str) -> Path:
    u = urlparse(url)
    if u.scheme != "https" or u.hostname not in HOSTS.values() or u.username or u.password or u.port:
        raise CloneError("only plain https URLs on github.com or gitlab.com are allowed")
    path = u.path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    host = "github" if u.hostname == "github.com" else "gitlab"
    if not valid_slug(path, host):
        raise CloneError("invalid repository path")
    name = path.split("/")[-1]
    if not _NAME.match(name):
        raise CloneError("invalid folder name")
    root = Path(dest_root).expanduser().resolve()
    target = (root / name).resolve()
    if target.parent != root:
        raise CloneError("destination escapes the chosen folder")
    if target.exists():
        raise CloneError(f"{target} already exists")
    return target


def clone(url: str, dest_root: Path | str, shallow: bool = True, runner=subprocess.run) -> Path:
    target = plan_clone(url, dest_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    args = ["git", "clone"] + (["--depth", "1"] if shallow else []) + ["--", url, str(target)]
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    r = runner(args, capture_output=True, text=True, timeout=600, env=env)
    if r.returncode != 0:
        raise CloneError((r.stderr or "git clone failed").strip().splitlines()[-1])
    return target
```

**Step 4:** Run `.venv/bin/pytest tests/test_clone.py -v`. Expected: PASS. If `github.com/a/-rf` unexpectedly passes, fix `_NAME` (it must reject a leading `-`).

**Step 5: Commit**

```bash
git add src/repohub/core/clone.py tests/test_clone.py
git commit -m "feat: add safe git clone" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 10: Shelves and the Hub facade

**Files:**
- Create: `src/repohub/core/browse.py`, `src/repohub/core/shelves.yaml`, `src/repohub/core/hub.py`, `src/repohub/config.py`, `tests/test_browse.py`, `tests/test_hub.py`
- Modify: `tests/helpers.py` (add `make_hub`, `FakeProvider.repo/readme/latest_release`)

**Step 1: Extend helpers**

Replace `FakeProvider` in `tests/helpers.py` and add `make_hub`:
```python
from repohub.core.cache import Cache
from repohub.core.hub import Hub
from repohub.core.store import Favorites


class FakeProvider:
    def __init__(self, host, repos=None, error=None, readme="# readme", release=None, detail_error=None):
        self.host, self.repos, self.error = host, repos or [], error
        self._readme, self._release, self.detail_error = readme, release, detail_error
        self.calls = 0

    async def search(self, query, filters, per_page=30):
        self.calls += 1
        if self.error:
            raise self.error
        return list(self.repos)

    async def repo(self, slug):
        self.calls += 1
        if self.detail_error:
            raise self.detail_error
        return next(r for r in self.repos if r.slug.lower() == slug.lower())

    async def readme(self, slug):
        return self._readme

    async def latest_release(self, slug):
        return self._release


def make_hub(*providers, clock=None):
    kw = {"now": clock} if clock else {}
    return Hub({p.host: p for p in providers}, Cache(**kw), Favorites(**kw))
```

**Step 2: Failing tests** (`tests/test_browse.py`)

```python
from repohub.core.browse import Shelf, load_shelves


def test_packaged_shelves_load_with_sane_defaults():
    shelves = load_shelves()
    assert len(shelves) >= 4
    assert all(s.name and (s.query or s.topic) for s in shelves)


def test_shelf_converts_to_filters(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text("- name: T\n  topic: tui\n  min_stars: 500\n  days: 90\n  language: Rust\n")
    s = load_shelves(p)[0]
    f = s.filters()
    assert (f.topic, f.min_stars, f.updated_within_days, f.language) == ("tui", 500, 90, "Rust")
```

`tests/test_hub.py`:
```python
from datetime import datetime, timezone

from helpers import FakeProvider, make_hub, mk
from repohub.core.browse import Shelf
from repohub.core.models import Asset, Release, SearchFilters
from repohub.core.providers.base import ProviderError, RateLimited


class Clock:
    t = 1_000_000.0

    def __call__(self):
        return self.t


async def test_search_is_cached_and_not_repeated():
    gh = FakeProvider("github", [mk("github", "a/a", 5)])
    hub = make_hub(gh)
    r1 = await hub.search("x")
    r2 = await hub.search("x")
    assert gh.calls == 1 and r1.repos == r2.repos


async def test_results_with_errors_are_not_cached():
    gh = FakeProvider("github", error=ProviderError("github", "boom"))
    hub = make_hub(gh)
    await hub.search("x")
    await hub.search("x")
    assert gh.calls == 2


async def test_all_hosts_failing_serves_stale_cache_with_flag():
    clock = Clock()
    gh = FakeProvider("github", [mk("github", "a/a", 5)])
    hub = make_hub(gh, clock=clock)
    await hub.search("x")
    clock.t += 10_000                                  # expire the cache
    gh.error = RateLimited("github", "rate limited")
    r = await hub.search("x")
    assert r.stale and [x.slug for x in r.repos] == ["a/a"] and r.errors == {"github": "rate limited"}


async def test_different_filters_use_different_cache_entries():
    gh = FakeProvider("github", [mk()])
    hub = make_hub(gh)
    await hub.search("x", SearchFilters(min_stars=1))
    await hub.search("x", SearchFilters(min_stars=2))
    assert gh.calls == 2


async def test_detail_combines_repo_readme_release_and_caches():
    rel = Release("v1", None, (Asset("a-arm64.tgz", 1, "u", "arm64"),))
    gh = FakeProvider("github", [mk("github", "o/r", 9)], readme="# hi", release=rel)
    hub = make_hub(gh)
    d = await hub.detail("github", "o/r")
    assert d.repo.stars == 9 and d.readme == "# hi" and d.release.has_arm64
    await hub.detail("github", "o/r")
    assert gh.calls == 1


async def test_detail_unknown_host_or_missing_repo_raises_provider_error():
    hub = make_hub(FakeProvider("github", [mk()]))
    for host, slug in (("nowhere", "o/r"),):
        try:
            await hub.detail(host, slug)
        except ProviderError:
            continue
        raise AssertionError("expected ProviderError")


async def test_detail_survives_readme_failure():
    class Boom(FakeProvider):
        async def readme(self, slug):
            raise ProviderError("github", "boom")

    hub = make_hub(Boom("github", [mk("github", "o/r")]))
    d = await hub.detail("github", "o/r")
    assert d.readme is None


async def test_shelf_search_uses_shelf_filters():
    gh = FakeProvider("github", [mk("github", "a/a", 500)])
    hub = make_hub(gh)
    r = await hub.shelf(Shelf(name="T", topic="tui", min_stars=100, days=365))
    assert [x.slug for x in r.repos] == ["a/a"]


async def test_refresh_favorites_updates_only_stale_entries():
    clock = Clock()
    gh = FakeProvider("github", [mk("github", "a/a", 1)])
    hub = make_hub(gh, clock=clock)
    hub.favorites.add(mk("github", "a/a", 1))
    gh.repos = [mk("github", "a/a", 77)]
    await hub.refresh_favorites(max_age=86400)
    assert hub.favorites.list()[0].stars == 1          # fresh: untouched
    clock.t += 90_000
    await hub.refresh_favorites(max_age=86400)
    assert hub.favorites.list()[0].stars == 77
```

**Step 3:** Run `.venv/bin/pytest tests/test_browse.py tests/test_hub.py -v`. Expected: FAIL (ImportError).

**Step 4: Implement**

`src/repohub/core/shelves.yaml`:
```yaml
- name: Terminal tools
  topic: tui
  min_stars: 500
  days: 365
- name: Local AI
  topic: llm
  min_stars: 1000
  days: 180
- name: Retro and emulation
  topic: emulator
  min_stars: 500
  days: 365
- name: Self-hosted
  topic: self-hosted
  min_stars: 1000
  days: 365
- name: Creative coding
  topic: creative-coding
  min_stars: 300
  days: 730
- name: Networking
  topic: networking
  min_stars: 1000
  days: 365
```

`src/repohub/core/browse.py`:
```python
from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from pathlib import Path

import yaml

from repohub.core.models import SearchFilters


@dataclass(frozen=True)
class Shelf:
    name: str
    query: str = ""
    topic: str | None = None
    language: str | None = None
    min_stars: int = 100
    days: int = 365

    def filters(self) -> SearchFilters:
        return SearchFilters(language=self.language, min_stars=self.min_stars,
                             updated_within_days=self.days, topic=self.topic)


def load_shelves(path: Path | str | None = None) -> list[Shelf]:
    text = Path(path).read_text() if path else resources.files("repohub.core").joinpath("shelves.yaml").read_text()
    return [Shelf(**item) for item in yaml.safe_load(text)]
```

`src/repohub/core/hub.py`:
```python
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import asdict, dataclass

from repohub.core.browse import Shelf
from repohub.core.cache import Cache
from repohub.core.models import Release, Repo, SearchFilters
from repohub.core.providers.base import ProviderError, RateLimited
from repohub.core.search import SearchResult, search_all
from repohub.core.store import Favorites

SEARCH_TTL = 600
DETAIL_TTL = 3600


@dataclass
class Detail:
    repo: Repo
    readme: str | None
    release: Release | None

    def to_dict(self) -> dict:
        return {"repo": self.repo.to_dict(), "readme": self.readme,
                "release": self.release.to_dict() if self.release else None}

    @classmethod
    def from_dict(cls, d: dict) -> "Detail":
        return cls(Repo.from_dict(d["repo"]), d["readme"], Release.from_dict(d["release"]) if d["release"] else None)


class Hub:
    def __init__(self, providers: dict, cache: Cache, favorites: Favorites):
        self.providers, self.cache, self.favorites = providers, cache, favorites

    async def search(self, query: str, filters: SearchFilters | None = None) -> SearchResult:
        filters = filters or SearchFilters()
        digest = hashlib.sha256(json.dumps([query, asdict(filters)], sort_keys=True).encode()).hexdigest()
        key = f"search:{digest}"
        hit = self.cache.get(key)
        if hit is not None:
            return SearchResult.from_dict(hit)
        result = await search_all(self.providers, query, filters)
        if not result.errors:
            self.cache.set(key, result.to_dict(), SEARCH_TTL)
        elif not result.repos:
            stale = self.cache.get(key, allow_stale=True)
            if stale is not None:
                old = SearchResult.from_dict(stale)
                return SearchResult(old.repos, result.errors, stale=True)
        return result

    async def shelf(self, shelf: Shelf) -> SearchResult:
        return await self.search(shelf.query, shelf.filters())

    async def detail(self, host: str, slug: str) -> Detail:
        provider = self.providers.get(host)
        if provider is None:
            raise ProviderError(host, "unknown host")
        key = f"detail:{host}:{slug.lower()}"
        hit = self.cache.get(key)
        if hit is not None:
            return Detail.from_dict(hit)
        try:
            repo, readme, release = await asyncio.gather(
                provider.repo(slug), provider.readme(slug), provider.latest_release(slug), return_exceptions=True)
            if isinstance(repo, BaseException):
                raise repo
        except RateLimited:
            stale = self.cache.get(key, allow_stale=True)
            if stale is not None:
                return Detail.from_dict(stale)
            raise
        readme = None if isinstance(readme, BaseException) else readme
        release = None if isinstance(release, BaseException) else release
        detail = Detail(repo, readme, release)
        self.cache.set(key, detail.to_dict(), DETAIL_TTL)
        return detail

    async def refresh_favorites(self, max_age: float = 86400) -> None:
        async def one(repo: Repo) -> None:
            provider = self.providers.get(repo.host)
            if provider is None:
                return
            try:
                self.favorites.update(await provider.repo(repo.slug))
            except ProviderError:
                pass

        await asyncio.gather(*(one(r) for r in self.favorites.stale(max_age)))
```

`src/repohub/config.py`:
```python
from __future__ import annotations

import os
from pathlib import Path

from platformdirs import user_data_dir

from repohub.core.auth import find_tokens
from repohub.core.cache import Cache
from repohub.core.hub import Hub
from repohub.core.providers.github import GitHubProvider
from repohub.core.providers.gitlab import GitLabProvider
from repohub.core.store import Favorites


def clone_root() -> Path:
    return Path(os.environ.get("REPOHUB_CLONE_DIR", "~/playground")).expanduser()


def build_hub() -> Hub:
    data = Path(user_data_dir("repohub"))
    data.mkdir(parents=True, exist_ok=True)
    db = str(data / "repohub.db")
    tokens = find_tokens()
    providers = {"github": GitHubProvider(tokens.github), "gitlab": GitLabProvider(tokens.gitlab)}
    return Hub(providers, Cache(db), Favorites(db))
```

**Step 5:** Run `.venv/bin/pytest -v`. Expected: all PASS.

**Step 6: Commit**

```bash
git add src tests
git commit -m "feat: add shelves and Hub facade with caching and stale fallback" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 11: Web app

**Files:**
- Create: `src/repohub/web/app.py`, `src/repohub/web/templates/{base,home,search,repo,favorites,clone_confirm}.html`, `src/repohub/web/templates/{_results,_fav_button,_message}.html`, `src/repohub/web/static/app.css`, `src/repohub/web/static/htmx.min.js`, `tests/test_web.py`

**Step 1: Vendor htmx**

Run: `mkdir -p src/repohub/web/static && curl -fsSL -o src/repohub/web/static/htmx.min.js https://cdn.jsdelivr.net/npm/htmx.org@2.0.4/dist/htmx.min.js && head -c 120 src/repohub/web/static/htmx.min.js`
Expected: JavaScript text (not HTML, not empty). If the download fails, stop and report.

**Step 2: Failing tests** (`tests/test_web.py`)

```python
import pytest
from fastapi.testclient import TestClient

from helpers import FakeProvider, make_hub, mk
from repohub.core.browse import Shelf
from repohub.core.clone import CloneError
from repohub.core.models import Asset, Release
from repohub.web.app import create_app, render_markdown

TOKEN = "test-token"


@pytest.fixture
def setup(tmp_path):
    rel = Release("v1", "2026-09-01T00:00:00Z", (Asset("t-aarch64.tgz", 5, "https://x/t", "arm64"),))
    gh = FakeProvider("github", [mk("github", "o/r", 50, description="<script>alert(1)</script>")],
                      readme="# Title\n\n<script>alert(1)</script>\n\n[bad](javascript:alert(1))", release=rel)
    hub = make_hub(gh)
    calls = []

    def cloner(url, root):
        calls.append((url, root))
        return tmp_path / "r"

    app = create_app(hub, tmp_path, session_token=TOKEN, shelves=[Shelf("Terminal tools", topic="tui")], cloner=cloner)
    return TestClient(app, base_url="http://localhost"), hub, calls


def test_home_lists_shelves(setup):
    client, *_ = setup
    r = client.get("/")
    assert r.status_code == 200 and "RepoHub" in r.text and "Terminal tools" in r.text


def test_shelf_fragment_renders_repos(setup):
    client, *_ = setup
    assert "o/r" in client.get("/shelf/0").text
    assert client.get("/shelf/9").status_code == 404


def test_search_escapes_descriptions(setup):
    client, *_ = setup
    r = client.get("/search", params={"q": "x"})
    assert "o/r" in r.text and "<script>alert(1)</script>" not in r.text and "&lt;script&gt;" in r.text


def test_search_rejects_bad_host(setup):
    client, *_ = setup
    assert client.get("/search", params={"q": "x", "host": "evil"}).status_code == 400


def test_repo_page_sanitizes_readme_and_shows_arm64_badge(setup):
    client, *_ = setup
    r = client.get("/repo/github/o/r")
    assert r.status_code == 200 and "Title" in r.text and "arm64" in r.text
    assert "<script>alert" not in r.text and "javascript:" not in r.text


def test_repo_page_rejects_invalid_slug(setup):
    client, *_ = setup
    assert client.get("/repo/github/o/r/extra").status_code == 404
    assert client.get("/repo/nowhere/o/r").status_code == 404


def test_render_markdown_strips_scripts_and_js_links():
    out = render_markdown("<script>x</script>\n\n[a](javascript:alert(1)) **b**")
    assert "<script" not in out and "javascript:" not in out and "<strong>b</strong>" in out


def test_favorite_requires_token_then_toggles(setup):
    client, hub, _ = setup
    assert client.get("/repo/github/o/r").status_code == 200
    assert client.post("/favorite", data={"host": "github", "slug": "o/r"}).status_code == 403
    assert client.post("/favorite", data={"host": "github", "slug": "o/r", "token": "wrong"}).status_code == 403
    assert client.post("/favorite", data={"host": "github", "slug": "o/r", "token": TOKEN}).status_code == 200
    assert hub.favorites.is_favorite("github:o/r")
    assert "o/r" in client.get("/favorites").text
    client.post("/favorite", data={"host": "github", "slug": "o/r", "token": TOKEN})
    assert not hub.favorites.is_favorite("github:o/r")


def test_clone_confirm_shows_destination_and_post_requires_token(setup, tmp_path):
    client, _, calls = setup
    r = client.get("/clone", params={"host": "github", "slug": "o/r"})
    assert r.status_code == 200 and str(tmp_path.resolve() / "r") in r.text
    assert client.post("/clone", data={"host": "github", "slug": "o/r"}).status_code == 403
    assert calls == []
    ok = client.post("/clone", data={"host": "github", "slug": "o/r", "token": TOKEN})
    assert ok.status_code == 200 and calls == [("https://github.com/o/r.git", tmp_path)]


def test_clone_error_is_shown_not_raised(tmp_path):
    hub = make_hub(FakeProvider("github", [mk()]))

    def cloner(url, root):
        raise CloneError("already exists")

    client = TestClient(create_app(hub, tmp_path, session_token=TOKEN, shelves=[], cloner=cloner), base_url="http://localhost")
    r = client.post("/clone", data={"host": "github", "slug": "o/r", "token": TOKEN})
    assert r.status_code == 200 and "already exists" in r.text


def test_foreign_host_header_is_rejected(setup):
    client, *_ = setup
    assert client.get("/", headers={"host": "evil.example"}).status_code == 400


def test_security_headers_present(setup):
    client, *_ = setup
    h = client.get("/").headers
    assert "script-src 'self'" in h["content-security-policy"] and h["x-content-type-options"] == "nosniff"


def test_static_htmx_is_served(setup):
    client, *_ = setup
    assert client.get("/static/htmx.min.js").status_code == 200
```

**Step 3:** Run `.venv/bin/pytest tests/test_web.py -v`. Expected: FAIL (ImportError).

**Step 4: Implement** (`src/repohub/web/app.py`)

```python
from __future__ import annotations

import argparse
import secrets
from pathlib import Path

import nh3
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from starlette.middleware.trustedhost import TrustedHostMiddleware

from repohub.core.browse import load_shelves
from repohub.core.clone import CloneError, clone as do_clone, clone_url, plan_clone
from repohub.core.models import SearchFilters
from repohub.core.providers.base import ProviderError, valid_slug

HERE = Path(__file__).parent
HOSTS = ("github", "gitlab")
CSP = ("default-src 'self'; img-src * data:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
       "frame-ancestors 'none'; form-action 'self'")
_md = MarkdownIt("commonmark", {"html": False})


def render_markdown(text: str) -> str:
    return nh3.clean(_md.render(text or ""), link_rel="noopener noreferrer")


def create_app(hub, clone_root, session_token: str | None = None, shelves=None, cloner=do_clone,
               allowed_hosts=("127.0.0.1", "localhost")) -> FastAPI:
    token = session_token or secrets.token_urlsafe(32)
    shelf_list = load_shelves() if shelves is None else shelves
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(allowed_hosts))
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers["Content-Security-Policy"] = CSP
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp

    def page(request: Request, name: str, status: int = 200, **ctx):
        return templates.TemplateResponse(request, name, {"token": token, **ctx}, status_code=status)

    def require_token(value: str) -> None:
        if not secrets.compare_digest(value or "", token):
            raise HTTPException(403, "missing or invalid session token")

    def require_repo(host: str, slug: str) -> None:
        if host not in HOSTS or not valid_slug(slug, host):
            raise HTTPException(404, "not found")

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request):
        return page(request, "home.html", shelves=list(enumerate(shelf_list)))

    @app.get("/shelf/{index}", response_class=HTMLResponse)
    async def shelf(request: Request, index: int):
        if not 0 <= index < len(shelf_list):
            raise HTTPException(404, "no such shelf")
        return page(request, "_results.html", result=await hub.shelf(shelf_list[index]), limit=6)

    @app.get("/search", response_class=HTMLResponse)
    async def search(request: Request, q: str = "", language: str = "", min_stars: int = 0, days: int = 0,
                     host: str = "both", archived: str = ""):
        if host != "both" and host not in HOSTS:
            raise HTTPException(400, "bad host")
        filters = SearchFilters(language=language or None, min_stars=max(min_stars, 0),
                                updated_within_days=days or None, hosts=HOSTS if host == "both" else (host,),
                                include_archived=bool(archived))
        result = await hub.search(q, filters)
        return page(request, "search.html", q=q, result=result, limit=None,
                    form=dict(language=language, min_stars=min_stars, days=days, host=host, archived=archived))

    @app.get("/repo/{host}/{slug:path}", response_class=HTMLResponse)
    async def repo_page(request: Request, host: str, slug: str):
        require_repo(host, slug)
        try:
            d = await hub.detail(host, slug)
        except ProviderError as e:
            return page(request, "_message.html", status=502, message=f"Could not load {slug}: {e}")
        return page(request, "repo.html", d=d, readme_html=render_markdown(d.readme) if d.readme else "",
                    is_fav=hub.favorites.is_favorite(d.repo.key))

    @app.get("/favorites", response_class=HTMLResponse)
    async def favorites_page(request: Request):
        await hub.refresh_favorites()
        return page(request, "favorites.html", repos=hub.favorites.list())

    @app.post("/favorite", response_class=HTMLResponse)
    async def toggle_favorite(request: Request, host: str = Form(...), slug: str = Form(...), token_field: str = Form("", alias="token")):
        require_token(token_field)
        require_repo(host, slug)
        key = f"{host}:{slug.lower()}"
        if hub.favorites.is_favorite(key):
            hub.favorites.remove(key)
            is_fav = False
        else:
            try:
                hub.favorites.add((await hub.detail(host, slug)).repo)
            except ProviderError as e:
                return page(request, "_message.html", status=502, message=str(e))
            is_fav = True
        return page(request, "_fav_button.html", host=host, slug=slug, is_fav=is_fav)

    @app.get("/clone", response_class=HTMLResponse)
    async def clone_confirm(request: Request, host: str, slug: str):
        require_repo(host, slug)
        try:
            target = plan_clone(clone_url(host, slug), clone_root)
        except CloneError as e:
            return page(request, "_message.html", message=str(e))
        return page(request, "clone_confirm.html", host=host, slug=slug, target=target)

    @app.post("/clone", response_class=HTMLResponse)
    async def clone_run(request: Request, host: str = Form(...), slug: str = Form(...), token_field: str = Form("", alias="token")):
        require_token(token_field)
        require_repo(host, slug)
        try:
            target = await run_in_threadpool(cloner, clone_url(host, slug), clone_root)
        except CloneError as e:
            return page(request, "_message.html", message=f"Clone failed: {e}")
        return page(request, "_message.html", message=f"Cloned to {target}")

    return app


def main() -> None:
    import uvicorn

    from repohub.config import build_hub, clone_root

    parser = argparse.ArgumentParser(prog="repohub-web")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if args.host not in ("127.0.0.1", "localhost"):
        print("WARNING: binding to a non-loopback address exposes clone and favorites to your network.")
    uvicorn.run(create_app(build_hub(), clone_root()), host=args.host, port=args.port)
```

Templates (all Jinja autoescape is on for `.html`):

`base.html`:
```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{% block title %}RepoHub{% endblock %}</title>
  <link rel="stylesheet" href="/static/app.css">
  <script src="/static/htmx.min.js" defer></script>
</head>
<body>
<header>
  <a class="brand" href="/">🌌 RepoHub</a>
  <form action="/search" method="get" class="searchbar">
    <input name="q" value="{{ q | default('') }}" placeholder="Search GitHub and GitLab" aria-label="Search" autofocus>
    <input name="language" value="{{ form.language | default('') }}" placeholder="language" aria-label="Language" size="9">
    <input name="min_stars" type="number" min="0" value="{{ form.min_stars | default(0) }}" aria-label="Minimum stars" title="Minimum stars">
    <input name="days" type="number" min="0" value="{{ form.days | default(0) }}" aria-label="Updated within days" title="Updated within N days (0 = any)">
    <select name="host" aria-label="Host">
      {% for h in ["both", "github", "gitlab"] %}<option value="{{ h }}" {% if form and form.host == h %}selected{% endif %}>{{ h }}</option>{% endfor %}
    </select>
    <label><input type="checkbox" name="archived" value="1" {% if form and form.archived %}checked{% endif %}> archived</label>
    <button>Search</button>
  </form>
  <nav><a href="/favorites">★ Favorites</a></nav>
</header>
<main>{% block body %}{% endblock %}</main>
</body>
</html>
```

`_results.html` (used by shelves and search; expects `result` and `limit`):
```html
{% if result.stale %}<p class="banner warn">Showing cached results.</p>{% endif %}
{% for host, msg in result.errors.items() %}<p class="banner warn">{{ host }}: {{ msg }}</p>{% endfor %}
<div class="grid">
{% for r in (result.repos[:limit] if limit else result.repos) %}
  <a class="card" href="/repo/{{ r.host }}/{{ r.slug }}">
    <div class="name">{{ r.slug }} <span class="badge {{ r.host }}">{{ r.host }}</span></div>
    <p>{{ r.description }}</p>
    <div class="meta">★ {{ r.stars }} · {{ r.language or "n/a" }} · {{ r.license or "no license" }}</div>
  </a>
{% else %}
  <p class="dim">No results.</p>
{% endfor %}
</div>
```

`home.html`:
```html
{% extends "base.html" %}
{% block body %}
{% for i, s in shelves %}
<section>
  <h2>{{ s.name }}</h2>
  <div hx-get="/shelf/{{ i }}" hx-trigger="load delay:{{ i * 400 }}ms" hx-swap="innerHTML"><p class="dim">Loading…</p></div>
</section>
{% endfor %}
{% endblock %}
```

`search.html`:
```html
{% extends "base.html" %}
{% block title %}{{ q }} - RepoHub{% endblock %}
{% block body %}<h2>Results for “{{ q }}”</h2>{% include "_results.html" %}{% endblock %}
```

`_fav_button.html`:
```html
<form class="fav" hx-post="/favorite" hx-target="this" hx-swap="outerHTML">
  <input type="hidden" name="host" value="{{ host }}"><input type="hidden" name="slug" value="{{ slug }}">
  <input type="hidden" name="token" value="{{ token }}">
  <button>{{ "★ Favorited" if is_fav else "☆ Favorite" }}</button>
</form>
```

`repo.html`:
```html
{% extends "base.html" %}
{% block title %}{{ d.repo.slug }} - RepoHub{% endblock %}
{% block body %}
{% set r = d.repo %}
<article class="detail">
  <h1>{{ r.slug }} <span class="badge {{ r.host }}">{{ r.host }}</span></h1>
  {% if r.archived %}<p class="banner warn">This repository is archived.</p>{% endif %}
  <p>{{ r.description }}</p>
  <p class="meta">★ {{ r.stars }} · ⑂ {{ r.forks }} · {{ r.language or "n/a" }} · {{ r.license or "no license" }} · last push {{ r.pushed_at[:10] }}</p>
  <div class="actions">
    {% with host=r.host, slug=r.slug %}{% include "_fav_button.html" %}{% endwith %}
    <a class="button" href="/clone?host={{ r.host }}&slug={{ r.slug | urlencode }}">Clone…</a>
    <a class="button" href="{{ r.url }}" rel="noopener noreferrer">Open on {{ r.host }}</a>
  </div>
  <div class="layout">
    <div class="readme">{% if readme_html %}{{ readme_html | safe }}{% else %}<p class="dim">No README.</p>{% endif %}</div>
    <aside>
      <h3>Topics</h3><p>{% for t in r.topics %}<span class="tag">{{ t }}</span> {% else %}<span class="dim">none</span>{% endfor %}</p>
      {% if r.homepage %}<p><a href="{{ r.homepage }}" rel="noopener noreferrer">Homepage</a></p>{% endif %}
      <h3>Latest release</h3>
      {% if d.release %}
        <p>{{ d.release.tag }} {% if d.release.has_arm64 %}<span class="badge ok">arm64</span>{% endif %}</p>
        <ul>{% for a in d.release.assets %}<li>{{ a.name }} <span class="dim">({{ a.arch }})</span></li>{% endfor %}</ul>
      {% else %}<p class="dim">No releases published.</p>{% endif %}
    </aside>
  </div>
</article>
{% endblock %}
```

`favorites.html`:
```html
{% extends "base.html" %}
{% block body %}<h2>Favorites</h2>
<div class="grid">
{% for r in repos %}
  <a class="card" href="/repo/{{ r.host }}/{{ r.slug }}"><div class="name">{{ r.slug }} <span class="badge {{ r.host }}">{{ r.host }}</span></div>
  <p>{{ r.description }}</p><div class="meta">★ {{ r.stars }} · {{ r.language or "n/a" }}</div></a>
{% else %}<p class="dim">No favorites yet.</p>{% endfor %}
</div>{% endblock %}
```

`clone_confirm.html`:
```html
{% extends "base.html" %}
{% block body %}
<h2>Clone {{ slug }}?</h2>
<p>This will create:</p><pre>{{ target }}</pre>
<p class="dim">Shallow clone. No install or build steps are run.</p>
<form hx-post="/clone" hx-target="#result" hx-swap="innerHTML">
  <input type="hidden" name="host" value="{{ host }}"><input type="hidden" name="slug" value="{{ slug }}">
  <input type="hidden" name="token" value="{{ token }}">
  <button>Confirm clone</button> <a class="button" href="/repo/{{ host }}/{{ slug }}">Cancel</a>
</form>
<div id="result"></div>
{% endblock %}
```

`_message.html`:
```html
<p class="banner">{{ message }}</p>
```
Note: when `_message.html` is returned as a full page (repo errors), it lacks base styling; acceptable for v1.

`static/app.css` (dark pastel, minimal):
```css
:root{--bg:#1e1e2e;--panel:#2a2a3d;--text:#cdd6f4;--dim:#9ca3c4;--lav:#b4befe;--pink:#f5c2e7;--mint:#a6e3a1;--peach:#fab387}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:16px/1.5 system-ui,sans-serif}
a{color:#89dceb}header{display:flex;flex-wrap:wrap;gap:12px;align-items:center;padding:12px 16px;background:var(--panel)}
.brand{color:var(--lav);font-weight:700;text-decoration:none}.searchbar{display:flex;flex-wrap:wrap;gap:6px;flex:1}
.searchbar input[name=q]{flex:1;min-width:180px}input,select,button,.button{background:var(--bg);color:var(--text);border:1px solid #3a3a55;border-radius:6px;padding:6px 10px;font:inherit}
button,.button{cursor:pointer;text-decoration:none;display:inline-block}button:hover,.button:hover{border-color:var(--lav)}
main{max-width:1100px;margin:0 auto;padding:16px}h1,h2,h3{color:var(--lav)}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px}
.card{display:block;background:var(--panel);border-radius:10px;padding:12px;color:var(--text);text-decoration:none;border:1px solid transparent}.card:hover{border-color:var(--lav)}
.name{color:var(--pink);font-weight:600;word-break:break-all}.meta,.dim{color:var(--dim);font-size:.9em}
.badge{font-size:.7em;padding:1px 6px;border-radius:99px;background:#3a3a55}.badge.ok{background:#22332b;color:var(--mint)}
.banner{background:#22303a;border-radius:6px;padding:8px 12px}.banner.warn{background:#3a2c24;color:var(--peach)}
.layout{display:grid;grid-template-columns:1fr 260px;gap:20px}.readme{overflow-wrap:anywhere}.readme img{max-width:100%}
.tag{background:#3a3a55;border-radius:6px;padding:1px 6px;font-size:.85em}.actions{display:flex;gap:8px;margin:12px 0}
@media (max-width:760px){.layout{grid-template-columns:1fr}}
```

**Step 5:** Run `.venv/bin/pytest tests/test_web.py -v`. Expected: PASS. If `TestClient` host handling differs, adjust only the tests' base_url, not the security behavior.

**Step 6: Manual check**

Run: `.venv/bin/repohub-web --port 8765 &` then `curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8765/`; then stop it with `kill %1`.
Expected: `200`. Open `http://127.0.0.1:8765` in a browser and confirm shelves load, search returns mixed GitHub/GitLab results, and a repo page shows its README.

**Step 7: Commit**

```bash
git add src/repohub/web tests/test_web.py
git commit -m "feat: add FastAPI web UI with sanitized READMEs and session-token protected actions" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 12: Terminal UI

**Files:**
- Create: `src/repohub/tui/app.py`, `tests/test_tui.py`

**Step 1: Failing tests** (`tests/test_tui.py`)

```python
from helpers import FakeProvider, make_hub, mk
from textual.widgets import DataTable, Input

from repohub.core.browse import Shelf
from repohub.core.models import Asset, Release
from repohub.tui.app import DetailScreen, RepoHubApp


def make_app(tmp_path, cloner=None):
    rel = Release("v1", None, (Asset("t-arm64.tgz", 1, "u", "arm64"),))
    gh = FakeProvider("github", [mk("github", "o/r", 50), mk("github", "p/q", 5)], release=rel)
    hub = make_hub(gh)
    return RepoHubApp(hub, tmp_path, shelves=[Shelf("Terminal tools", topic="tui")],
                      cloner=cloner or (lambda url, root: tmp_path / "r")), hub


async def settle(app, pilot):
    await app.workers.wait_for_complete()
    await pilot.pause()


async def test_starts_with_shelf_list(tmp_path):
    app, _ = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.query_one(DataTable).row_count == 1


async def test_search_fills_results(tmp_path):
    app, _ = make_app(tmp_path)
    async with app.run_test() as pilot:
        app.query_one(Input).focus()
        await pilot.press(*"tui", "enter")
        await settle(app, pilot)
        assert app.query_one(DataTable).row_count == 2


async def test_selecting_a_repo_opens_detail_and_toggles_favorite(tmp_path):
    app, hub = make_app(tmp_path)
    async with app.run_test() as pilot:
        app.query_one(Input).focus()
        await pilot.press(*"tui", "enter")
        await settle(app, pilot)
        app.query_one(DataTable).focus()
        await pilot.press("enter")
        await settle(app, pilot)
        assert isinstance(app.screen, DetailScreen)
        assert app.screen.detail.repo.slug == "o/r" and app.screen.detail.release.has_arm64
        await pilot.press("f")
        await pilot.pause()
        assert hub.favorites.is_favorite("github:o/r")
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, DetailScreen)


async def test_clone_requires_confirmation(tmp_path):
    calls = []
    app, _ = make_app(tmp_path, cloner=lambda url, root: calls.append((url, root)) or tmp_path / "r")
    async with app.run_test() as pilot:
        app.query_one(Input).focus()
        await pilot.press(*"tui", "enter")
        await settle(app, pilot)
        app.query_one(DataTable).focus()
        await pilot.press("enter")
        await settle(app, pilot)
        await pilot.press("c")
        await pilot.pause()
        assert calls == []                      # nothing runs until confirmed
        await pilot.press("n")
        await settle(app, pilot)
        assert calls == []
        await pilot.press("c")
        await pilot.pause()
        await pilot.press("y")
        await settle(app, pilot)
        assert calls == [("https://github.com/o/r.git", tmp_path)]
```

**Step 2:** Run `.venv/bin/pytest tests/test_tui.py -v`. Expected: FAIL (ImportError).

**Step 3: Implement** (`src/repohub/tui/app.py`)

```python
from __future__ import annotations

import asyncio

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen, Screen
from textual.widgets import DataTable, Footer, Header, Input, Markdown, Static

from repohub.core.browse import load_shelves
from repohub.core.clone import CloneError, clone as do_clone, clone_url, plan_clone
from repohub.core.providers.base import ProviderError


class ConfirmClone(ModalScreen[bool]):
    BINDINGS = [Binding("y", "confirm", "Yes"), Binding("n", "cancel", "No"), Binding("escape", "cancel", "No")]

    def __init__(self, target):
        super().__init__()
        self.target = target

    def compose(self) -> ComposeResult:
        yield Static(f"Clone into:\n{self.target}\n\ny = confirm    n = cancel", markup=False)

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class DetailScreen(Screen):
    BINDINGS = [Binding("escape", "app.pop_screen", "Back"), Binding("f", "favorite", "Favorite"),
                Binding("c", "clone", "Clone")]

    def __init__(self, hub, host: str, slug: str, clone_root, cloner):
        super().__init__()
        self.hub, self.host, self.slug, self.clone_root, self.cloner = hub, host, slug, clone_root, cloner
        self.detail = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Loading…", id="meta", markup=False)
        yield Markdown("", id="readme")
        yield Footer()

    def on_mount(self) -> None:
        self.run_worker(self._load(), exclusive=True)

    async def _load(self) -> None:
        meta = self.query_one("#meta", Static)
        try:
            self.detail = await self.hub.detail(self.host, self.slug)
        except ProviderError as e:
            meta.update(f"Could not load {self.slug}: {e}")
            return
        r, rel = self.detail.repo, self.detail.release
        release = f"{rel.tag}{' (arm64 available)' if rel.has_arm64 else ''}" if rel else "no releases"
        star = "★ favorited" if self.hub.favorites.is_favorite(r.key) else ""
        meta.update(f"{r.slug} [{r.host}]  ★ {r.stars}  {r.language or 'n/a'}  {r.license or 'no license'}  {star}\n"
                    f"{r.description}\nRelease: {release}")
        await self.query_one("#readme", Markdown).update(self.detail.readme or "_No README._")

    def action_favorite(self) -> None:
        if self.detail is None:
            return
        key = self.detail.repo.key
        if self.hub.favorites.is_favorite(key):
            self.hub.favorites.remove(key)
            self.notify("Removed from favorites")
        else:
            self.hub.favorites.add(self.detail.repo)
            self.notify("Added to favorites")

    def action_clone(self) -> None:
        try:
            url = clone_url(self.host, self.slug)
            target = plan_clone(url, self.clone_root)
        except CloneError as e:
            self.notify(str(e), severity="error")
            return

        def done(confirmed: bool | None) -> None:
            if confirmed:
                self.run_worker(self._run_clone(url), exclusive=False)

        self.app.push_screen(ConfirmClone(target), done)

    async def _run_clone(self, url: str) -> None:
        try:
            target = await asyncio.to_thread(self.cloner, url, self.clone_root)
        except CloneError as e:
            self.notify(f"Clone failed: {e}", severity="error")
        else:
            self.notify(f"Cloned to {target}")


class RepoHubApp(App):
    TITLE = "RepoHub"
    BINDINGS = [Binding("ctrl+f", "favorites", "Favorites"), Binding("escape", "home", "Shelves")]

    def __init__(self, hub, clone_root, shelves=None, cloner=do_clone):
        super().__init__()
        self.hub, self.clone_root, self.cloner = hub, clone_root, cloner
        self.shelves = load_shelves() if shelves is None else shelves

    def compose(self) -> ComposeResult:
        yield Header()
        yield Input(placeholder="Search GitHub and GitLab, then press Enter", id="q")
        yield Static("", id="status", markup=False)
        yield DataTable(cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        self.action_home()

    def _table(self) -> DataTable:
        return self.query_one(DataTable)

    def _status(self, text: str) -> None:
        self.query_one("#status", Static).update(text)

    def action_home(self) -> None:
        t = self._table()
        t.clear(columns=True)
        t.add_columns("Shelf", "Topic")
        for i, s in enumerate(self.shelves):
            t.add_row(s.name, s.topic or s.query, key=f"shelf:{i}")
        self._status("Shelves. Press Enter on one, or type a search above.")

    def action_favorites(self) -> None:
        self.run_worker(self._show_favorites(), exclusive=True)

    async def _show_favorites(self) -> None:
        await self.hub.refresh_favorites()
        self._show_repos(self.hub.favorites.list(), "Favorites")

    def _show_repos(self, repos, status: str) -> None:
        t = self._table()
        t.clear(columns=True)
        t.add_columns("Repo", "Host", "Stars", "Lang", "Description")
        for r in repos:
            t.add_row(r.slug, r.host, str(r.stars), r.language or "n/a", r.description[:70], key=f"repo:{r.host}:{r.slug}")
        self._status(status)

    def _show_result(self, result, label: str) -> None:
        notes = [label]
        if result.stale:
            notes.append("(cached)")
        notes += [f"{h}: {m}" for h, m in result.errors.items()]
        self._show_repos(result.repos, "  ".join(notes))

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        query = event.value.strip()
        if query:
            self._status("Searching…")
            self.run_worker(self._search(query), exclusive=True)

    async def _search(self, query: str) -> None:
        self._show_result(await self.hub.search(query), f"Results for '{query}'")

    async def _open_shelf(self, index: int) -> None:
        shelf = self.shelves[index]
        self._show_result(await self.hub.shelf(shelf), shelf.name)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        key = event.row_key.value or ""
        if key.startswith("shelf:"):
            self._status("Loading shelf…")
            self.run_worker(self._open_shelf(int(key.split(":")[1])), exclusive=True)
        elif key.startswith("repo:"):
            _, host, slug = key.split(":", 2)
            self.push_screen(DetailScreen(self.hub, host, slug, self.clone_root, self.cloner))


def main() -> None:
    from repohub.config import build_hub, clone_root

    RepoHubApp(build_hub(), clone_root()).run()
```

**Step 4:** Run `.venv/bin/pytest tests/test_tui.py -v`. Expected: PASS. Textual APIs shift between versions. If a test fails on an API detail (for example `run_test`, `workers.wait_for_complete`, `DataTable.clear(columns=True)`, `Static.update`), check the installed version's docs (`.venv/bin/pip show textual`) and adjust the smallest thing; do not weaken the assertions. Clone confirmation and favorite toggling assertions must stay.

**Step 5: Manual check**

Run `.venv/bin/repohub-tui` in a real terminal. Confirm shelves show, `/` style search returns mixed hosts, Enter opens a detail screen with README, `f` favorites, `c` shows the destination and asks before cloning, Escape goes back, Ctrl+Q quits.

**Step 6: Commit**

```bash
git add src/repohub/tui tests/test_tui.py
git commit -m "feat: add Textual TUI with confirmed cloning" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 13: README, live smoke test, final verification

**Files:**
- Create: `README.md`, `tests/test_live.py`
- Modify: `docs/plans/2026-09-20-repohub-design.md` (record deviations)

**Step 1: Live smoke test** (`tests/test_live.py`), excluded by default

```python
import pytest

from repohub.core.auth import find_tokens
from repohub.core.models import SearchFilters
from repohub.core.providers.github import GitHubProvider
from repohub.core.providers.gitlab import GitLabProvider

pytestmark = pytest.mark.live


async def test_github_live_search_and_detail():
    p = GitHubProvider(find_tokens().github)
    repos = await p.search("ripgrep", SearchFilters(min_stars=1000))
    assert repos and repos[0].host == "github"
    assert (await p.repo(repos[0].slug)).slug == repos[0].slug


async def test_gitlab_live_search():
    p = GitLabProvider(find_tokens().gitlab)
    repos = await p.search("wireshark", SearchFilters())
    assert repos and all(r.host == "gitlab" for r in repos)
```

Run: `.venv/bin/pytest -m live -v`
Expected: PASS with network access. A rate-limit failure without a token is acceptable to report, not to hide.

**Step 2: README.md**

Write a short README covering: what RepoHub is; install (`uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -e ".[dev]"`); run (`.venv/bin/repohub-web`, `.venv/bin/repohub-tui`); tokens (`GITHUB_TOKEN`/`GH_TOKEN`/`gh auth token`, `GITLAB_TOKEN`; tokens are held in memory, sent only to the matching API host, never logged); `REPOHUB_CLONE_DIR` (default `~/playground`); editing `src/repohub/core/shelves.yaml`; security notes (loopback bind, session token on actions, README sanitizing, clone allow-list); running tests (`.venv/bin/pytest`, `-m live` for the live tests). State plainly what v1 does not do (see design doc "Out of scope").

**Step 3: Record deviations in the design doc**

Append a "Changes made during implementation planning" section to `docs/plans/2026-09-20-repohub-design.md` listing the four deviations from the header of this plan.

**Step 4: Full verification**

Run: `.venv/bin/pytest -v`
Expected: every test PASSES, live tests deselected.

Run: `.venv/bin/repohub-web --port 8765 &` then `curl -s http://127.0.0.1:8765/ | head -5`, then `kill %1`.
Expected: HTML starting with `<!doctype html>`.

Run: `git status --short`
Expected: clean except intended files.

**Step 5: Commit**

```bash
git add README.md tests/test_live.py docs
git commit -m "docs: add README and live smoke tests, record design changes" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Risks to watch during execution

- **Dependency wheels on aarch64:** `nh3` and `textual` ship wheels for aarch64 Linux as far as I know, but this is unverified. If install fails, stop and report the package name.
- **Textual API drift:** Task 12 tests use Pilot and `workers.wait_for_complete`; adjust API details only, keep the assertions.
- **GitHub search rate limits:** the home page fires one search per shelf, staggered by 400 ms; without a token, only about 10 searches a minute are allowed. Six shelves plus a search can hit the limit; the UI shows a per-host banner and serves cached results.
- **GitLab search:** filters GitLab cannot express (minimum stars) are applied client-side, so a page of 30 may shrink after filtering.
- **Remote images in READMEs:** the CSP allows `img-src *`, so viewing a README can load images from third-party hosts. Accepted for v1; noted in the README.
