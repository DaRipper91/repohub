# RepoHub Phase 1 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (or executing-plans) to implement this plan task-by-task.

**Goal:** Add filters and sorting to both apps, curated shelves (with the 171-project catalog), and a scriptable `repohub` CLI.

**Architecture:** New pure-core pieces (`queryparse.py`, richer `browse.py`, `Hub.curated_page`, `cli.py`) sit under the existing front ends. Both apps call the same parser and the same Hub methods. Design: `docs/plans/2026-09-21-phase1-design.md`.

**Tech Stack:** unchanged (Python 3.11+, httpx, FastAPI/Jinja/htmx, Textual, SQLite, PyYAML, platformdirs, pytest, pytest-asyncio, respx).

---

## Conventions (apply to every task)

- Work in `/home/daripper/Projects/repohub` on branch `main`. Use the project venv: `.venv/bin/pytest`, `.venv/bin/python`. Machine: Fedora Asahi aarch64, 7 GB RAM; keep everything light.
- TDD: write failing tests first, run them and confirm they fail for the right reason, implement the minimum, run the whole suite (`.venv/bin/pytest -q`; baseline before Task 1 is **234 passed, 2 deselected**), commit.
- Every commit ends with the trailer, using two `-m` flags: `git commit -m "feat: ..." -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"`.
- No test touches the real network, real git, real tokens, or the real user data or config directories. Never call `repohub.config.build_hub()` in a test. Never start a server or the real TUI in a test (use Textual's `run_test`, FastAPI's `TestClient(app, base_url="http://localhost")`).
- Untrusted text (API data, YAML notes, README text) is never interpreted as markup. Terminal cells are `rich.text.Text(...)`; web templates rely on autoescape; provider data goes through `clean_text` (`src/repohub/core/textsafe.py`).
- Do not change the clone protections (`src/repohub/core/clone.py`). Do not push to any remote.
- Do not weaken an existing test. If a plan test contains a genuine bug, fix it minimally and say so in your report.
- Existing helpers: `tests/helpers.py` has `mk(...)` (builds a `Repo`), `FakeProvider`, and `make_hub(*providers)`.

---

### Task 1: Model fields (`fork`, `sort`, `hide_forks`)

**Files:** Modify `src/repohub/core/models.py`, `tests/helpers.py`; Test `tests/test_models.py`.

Changes:
- `Repo` gets a new LAST field `fork: bool = False` (it must come after the non-default fields). `to_dict`/`from_dict` already work through `asdict`/`cls(**d)`; a cached row without the key must still load and give `fork=False`.
- `SearchFilters` gets `sort: str = "stars"` and `hide_forks: bool = False`.
- Add a module constant `SORTS = ("stars", "updated", "forks")`.
- `tests/helpers.py`: `mk` must accept `fork=` through its `**kw` (it already merges kw into the Repo kwargs; verify) and `FakeProvider.search` must record `self.last_filters = filters` and `self.last_query = query` for later tests.

Tests to add in `tests/test_models.py`:
- `Repo.from_dict(row)` where `row = make_repo().to_dict()` with the `"fork"` key deleted gives `.fork is False` (old cached rows still load).
- `Repo.from_dict(make_repo(fork=True).to_dict()).fork is True`.
- `SearchFilters()` has `sort == "stars"` and `hide_forks is False`; `SORTS == ("stars", "updated", "forks")`.
- The search cache key changes with the new fields: `asdict(SearchFilters(sort="updated")) != asdict(SearchFilters())` (documents that Hub's cache key covers them).

Commit: `feat: add fork, sort and hide_forks to the models`.

---

### Task 2: Provider support (sort, forks)

**Files:** Modify `src/repohub/core/providers/github.py`, `src/repohub/core/providers/gitlab.py`; Test `tests/test_github.py`, `tests/test_gitlab.py`.

GitHub (`github.py`):
- `_to_repo`: add `fork=bool(item.get("fork"))`.
- `search`: `params["sort"]` becomes `{"stars": "stars", "updated": "updated", "forks": "forks"}.get(filters.sort, "stars")` (an unknown value falls back to stars); when `filters.hide_forks` append the qualifier `fork:false` to `q`.

GitLab (`gitlab.py`):
- `_to_repo`: add `fork=bool(item.get("forked_from_project"))`.
- `search`: `order_by` becomes `"last_activity_at"` for `sort="updated"` and `"star_count"` otherwise (GitLab cannot order by forks: `sort="forks"` is applied client-side later, so fetch by stars). `hide_forks` is NOT sent to GitLab (client-side, Task 3).

Tests (respx, no network):
- GitHub: `sort="updated"` sends `sort=updated`; `sort="forks"` sends `sort=forks`; an invalid sort falls back to `stars`; `hide_forks=True` puts `fork:false` in `q` and the default does not; an item with `"fork": true` maps to `Repo.fork is True`, an item without the key gives `False`.
- GitLab: `sort="updated"` sends `order_by=last_activity_at`; default and `forks` send `order_by=star_count`; an item with a truthy `forked_from_project` maps to `fork=True`, without it `False`; `hide_forks` adds no parameter.

Commit: `feat: pass sort and fork data through the providers`.

---

### Task 3: Merge, hide forks and final sort

**Files:** Modify `src/repohub/core/search.py`; Test `tests/test_search.py`.

Changes in `search.py`:
- `_keep`: add `if f.hide_forks and repo.fork: return False`.
- Replace the final `sorted(...)` with an `_ordered(repos, sort)` helper that first sorts by the tie-break `(repo.host != "github", repo.slug.lower())` and then does a STABLE sort on the chosen primary key with `reverse=True`:
  - `"updated"`: `key=lambda r: r.pushed_at` (ISO strings sort correctly; an empty value sorts last);
  - `"forks"`: `key=lambda r: r.forks`;
  - anything else (`"stars"`): `key=lambda r: r.stars`.
  Keep the existing dedupe logic (it decides which duplicate survives by stars) unchanged.

Tests (use `FakeProvider` and the `run` helper style already in `tests/test_search.py`):
- `sort="updated"` orders by `pushed_at` descending, ties github-first, and a repo with `pushed_at=""` is last.
- `sort="forks"` orders by `forks` descending across both hosts (interleaved order, not per host).
- default sort is stars (existing tests keep passing).
- `hide_forks=True` removes repos with `fork=True` from both hosts; without it they stay.
- A unit test that `_ordered` is stable: equal primary keys keep the github-first, then slug order.

Commit: `feat: sort merged results and hide forks client-side`.

---

### Task 4: Shared query parser

**Files:** Create `src/repohub/core/queryparse.py`, `tests/test_queryparse.py`.

Behaviour: `parse_query(raw, base=None)` extracts `key:value` tokens and flags from the search text and returns the remaining words as the search text. Keys are case-insensitive: `lang`/`language`, `stars`, `days`, `host`, `sort`, `topic`; flags: `nofork`, `archived`. A value for `stars` may start with `>` or `>=` (both are accepted and ignored). Unknown `key:value` tokens (for example `http://x`, `rust:lang`) stay as plain words. A known key with a bad value produces a problem message and the token is dropped; the rest of the query still works. Tokens split on whitespace (no quoting).

Full code:

```python
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
    try:
        n = int(value)
    except ValueError:
        raise ValueError(f"{name} needs a whole number, got '{value}'") from None
    if not 0 <= n <= limit:
        raise ValueError(f"{name} must be between 0 and {limit}")
    return n


def _apply(filters: SearchFilters, key: str, value: str) -> SearchFilters:
    if key == "language":
        if not _LANG.match(value):
            raise ValueError(f"lang has unsupported characters: '{value}'")
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
            raise ValueError(f"topic has unsupported characters: '{value}'")
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
```

Tests (`tests/test_queryparse.py`), all pure:
- `parse_query("tui lang:rust stars:500 days:90 host:github sort:updated nofork archived")` gives text `"tui"`, `language="Rust"`-case preserved as typed (`"rust"`), `min_stars=500`, `updated_within_days=90`, `hosts=("github",)`, `sort="updated"`, `hide_forks=True`, `include_archived=True`, no problems.
- Case-insensitive keys and flags: `LANG:Go STARS:>=10 NoFork` works.
- `host:both` restores both hosts; `days:0` gives `updated_within_days=None`.
- Unknown keys stay as words: `parse_query("http://x rust:lang foo")` text `"http://x rust:lang foo"`.
- Bad values give a problem and keep the rest working: `"tui stars:abc sort:bogus host:nowhere lang:$$"` gives text `"tui"`, four problems, filters unchanged from default.
- Out of range: `stars:99999999999`, `days:99999` produce problems and no crash.
- `lang:` (empty value) gives the "needs a value" problem.
- `base` is respected and overridden: `parse_query("stars:5", SearchFilters(min_stars=100, language="go"))` gives `min_stars=5`, `language="go"`.
- Empty and whitespace-only input and `None`-safe: `parse_query("")` and `parse_query("   ")` give empty text and default filters.
- Very long input (100,000 characters of words) returns quickly.

Commit: `feat: add a shared query-syntax parser`.

---

### Task 5: Web filters, sorting and query syntax

**Files:** Modify `src/repohub/web/app.py`, `src/repohub/web/templates/base.html`, `src/repohub/web/templates/search.html`, `src/repohub/web/static/app.css` (only if needed); Test `tests/test_web.py`.

Changes:
- `/search` gets two more query parameters: `sort: str = "stars"` (must be in `SORTS`, else `HTTPException(400, "bad sort")`) and `hide_forks: str = ""`.
- Build the base filters from the form fields exactly as today plus `sort` and `hide_forks=bool(hide_forks)`; then `parsed = parse_query(q, base)`; call `hub.search(parsed.text, parsed.filters)`. The page still shows the ORIGINAL `q` in the search box. Pass `problems=parsed.problems` to `search.html`.
- `search.html`: when `problems` is non-empty render each as a `<p class="banner warn">Ignored: {{ problem }}</p>` (autoescaped).
- `base.html` search bar: add a sort `<select name="sort">` (options stars, updated, forks; keeps the selected value from `form.sort`) and a "hide forks" checkbox (`name="hide_forks" value="1"`, checked when `form.hide_forks`). Keep every existing control and placeholder. Pass `sort` and `hide_forks` in the `form` dict the route hands to the template.

Tests (fake providers; use the `FakeProvider.last_filters` / `last_query` recorded in Task 1):
- `/search?q=tui lang:rust stars:500 nofork sort:updated` calls the provider with query `"tui"`, `language="rust"`, `min_stars=500`, `hide_forks=True`, `sort="updated"`.
- Form fields and syntax combine: `/search?q=x&sort=forks&hide_forks=1` gives filters `sort="forks"`, `hide_forks=True`; a token in `q` overrides the form value.
- A bad token (`stars:abc`) still returns 200 with a "Ignored:" banner containing the message, and the original query is kept in the input.
- `/search?sort=bogus` returns 400.
- The rendered search bar contains the sort select with the chosen option selected and a checked hide-forks checkbox when set.
- A hostile token value (`lang:<script>`) is escaped in the banner (no raw `<script>`).

Commit: `feat(web): sort, hide-forks and query syntax in search`.

---

### Task 6: Terminal app query syntax

**Files:** Modify `src/repohub/tui/app.py`; Test `tests/test_tui.py`.

Changes:
- `_search(query)` parses with `parse_query(query)`, calls `self.hub.search(parsed.text, parsed.filters)`, and shows problems in the status line via the existing `_status` (plain text, `markup=False` is already set): `Ignored: <problem>; <problem>` appended to the results label. Keep the existing exception handling.
- Change the Input placeholder to `Search GitHub and GitLab (e.g. tui lang:rust stars:500 days:90 host:github sort:updated nofork)`.
- Keep the results label using the original query text.

Tests (Textual `run_test`, `make_hub` with `FakeProvider` recording `last_filters`):
- Typing `tui lang:rust nofork sort:forks` and pressing Enter reaches the provider with `last_query == "tui"`, `language == "rust"`, `hide_forks`, `sort == "forks"`.
- A bad token (`stars:abc`) still fills the table and the status text contains `Ignored` and the message.
- A query that is only syntax (`nofork`) gives an empty text query and still runs.

Commit: `feat(tui): query syntax for filters and sorting`.

---

### Task 7: Shelf model, loader and personal shelves

**Files:** Rewrite `src/repohub/core/browse.py`; Modify `src/repohub/core/shelves.yaml` only if needed (it must keep loading); Test `tests/test_browse.py`.

Keep `Shelf` backward compatible (existing search-based fields and `filters()`), and keep `load_shelves(path=None)` working with the same errors. Add curated shelves and the loader for all sources. Full code for the new pieces (keep the existing `Shelf` fields; these are the additions and helpers):

```python
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


def personal_shelves_path() -> Path:
    return Path(user_config_dir("repohub")) / "shelves.yaml"
```

Required behaviour (implement, and cover with tests):
- YAML shelf keys: the existing ones plus `repos` (list) and `as_of` (string). If `repos` is present the shelf is curated and the search fields are ignored.
- A `repos` entry is either a string `"host:owner/name"` or a mapping with `repo` (required), optional `note` and `snapshot` (mapping with only the `Snapshot` field names; `stars` must be a non-negative int, the others strings). Unknown keys are an error.
- Validation errors are `ValueError` with a message naming the shelf and the entry number, for example `shelf 'X' entry #3: unknown host 'bitbucket'`. Checks: host in `HOSTS`, `valid_slug(slug, host)`, duplicate entries in one shelf, wrong types, unknown keys. `note` and snapshot strings go through `clean_text` and are truncated to `MAX_NOTE` / `MAX_DESC`; a snapshot `stars` above 10,000,000 is an error.
- `parse_shelves(data, source="shelves")` does the validation; `load_shelves(path=None)` keeps its current behaviour (path or the packaged `shelves.yaml`), now via `parse_shelves`. Keep the existing error messages tested in `tests/test_browse.py` (for example `invalid shelf #0` wording for a missing `name`/unknown key): do not weaken those tests.
- `load_all_shelves(personal_path=None) -> LoadedShelves`: shelves from the packaged `shelves.yaml`, then the packaged `catalog_shelves.yaml` if that file exists (Task 9 adds it; a missing packaged catalog is not an error), then the personal file (`personal_path` or `personal_shelves_path()`). A missing personal file is fine and silent. A personal file that fails to parse or validate does NOT raise: add one problem string such as `personal shelves (<path>): <error>` and skip that file. A problem in the packaged files still raises (it is a bug).

Tests (`tmp_path` only; never the real config dir): both shelf kinds load; entry as a string and as a mapping; snapshot parsing; each validation error (bad host, bad slug like `../x` and `o/r\n`, duplicate, wrong types, unknown keys, huge stars, oversized note is truncated and control characters are stripped); `curated` property; load order (defaults, catalog when present, personal last); a broken personal file yields a problem and the defaults still load; a missing personal file yields no problem; the existing search-shelf tests keep passing.

Commit: `feat: curated shelves and a personal shelves file`.

---

### Task 8: Hub support for curated shelves

**Files:** Modify `src/repohub/core/hub.py`; Test `tests/test_hub.py`; `tests/helpers.py` if a hook is needed.

Add to `hub.py`:
- `CuratedItem` dataclass: `repo: Repo`, `note: str`, `as_of: str | None`, `live: bool`.
- `CuratedPage` dataclass: `items: list[CuratedItem]`, `total: int`, `offset: int`, `limit: int`, `errors: dict[str, str]`.
- `repo_from_snapshot(entry, as_of)` helper (module level): builds a `Repo` with `host`, `slug`, canonical `url` (`https://github.com/{slug}` / `https://gitlab.com/{slug}`), snapshot description/stars/language/license, `pushed_at` from the snapshot, `topics=()`, `archived=False`, `forks=0`, `homepage=""`, `fork=False`; with no snapshot use empty strings and zero stars.
- `Hub.repo_summary(host, slug) -> Repo`: cached 1 hour under key `repo:{host}:{slug.lower()}` (store `Repo.to_dict()`); on a miss call `provider.repo(slug)`; unknown host raises `ProviderError(host, "unknown host")`; provider errors propagate.
- `Hub.curated_page(shelf, offset=0, limit=12) -> CuratedPage`: slice `shelf.repos[offset:offset+limit]` (clamp offset/limit to sane bounds: `offset >= 0`, `1 <= limit <= 50`); build snapshot repos immediately; refresh the visible entries concurrently through `repo_summary` bounded by an `asyncio.Semaphore(REFRESH_CONCURRENCY)`; on success replace the item's repo and set `live=True`; on `ProviderError` keep the snapshot (`live=False`) and record the message in `errors[host]` (one message per host, first wins); any other `Exception` from a refresh counts as `"unexpected error"` for that host and the snapshot stays; `asyncio.CancelledError`/`BaseException` must propagate. `note` comes from the entry, `as_of` from the shelf.
- `Hub.shelf(shelf)` for a curated shelf returns `SearchResult([item.repo for item in page.items], page.errors)` using `curated_page(shelf, 0, 12)`; for a search shelf it behaves as today.

Tests (fake providers, injected clock via `make_hub(..., clock=...)` where useful):
- A curated shelf of 30 entries: `curated_page(shelf, 0, 12)` returns 12 items and `total == 30`; page 3 (`offset=24`) returns 6; out-of-range offset returns an empty page; `limit=0` and `limit=1000` are clamped.
- Snapshot-only when every refresh fails: repos equal the snapshot values, `live` False, `errors` has the host message.
- A successful refresh replaces stars/description with the provider's values and sets `live=True`; a partial failure mixes both.
- Refresh concurrency never exceeds 8 (provider records peak concurrent calls, 30 entries and `limit=30`... use `limit=30` only if `limit` clamp allows; otherwise use 3 pages).
- `repo_summary` caches for the TTL (second call does not hit the provider) and refetches after the clock passes the TTL.
- A `RuntimeError` from a provider's `repo()` during refresh keeps the snapshot and reports `"unexpected error"`; a `CancelledError` propagates.
- `Hub.shelf` on a search shelf still works (existing test) and on a curated shelf returns the first page's repos.

Commit: `feat: Hub.curated_page with snapshot-first, lazy-refresh shelves`.

---

### Task 9: Catalog shelves

**Files:** Move `docs/plans/phase1-catalog-shelves.yaml` to `src/repohub/core/catalog_shelves.yaml` with `git mv`; Modify `src/repohub/config.py` no; Test `tests/test_browse.py` (or a new `tests/test_catalog.py`).

The data file was generated once from the 2026-09-20 project catalog: 8 shelves named `Catalog: <category>`, 171 entries in total, each with a `repo`, a `note` and a `snapshot`, and `as_of: '2026-09-20'`. Do not edit its contents.

Steps: write the tests, run them (they fail because the file is not in the package), `git mv` the file, run them again.

Tests:
- `load_all_shelves(personal_path=tmp_path / "none.yaml")` includes the 8 catalog shelves after the defaults, `problems == []`.
- Total entries across catalog shelves is 171; every entry passes validation (the loader already enforces it); every entry has a non-empty note and a snapshot; every shelf has `as_of == "2026-09-20"`; both hosts occur (`gitlab` entries exist); no duplicate `key` across the whole catalog.
- All notes and descriptions contain no control characters.
- The file is packaged: `resources.files("repohub.core").joinpath("catalog_shelves.yaml").is_file()`.

Commit: `feat: package the 171-project catalog as curated shelves`.

---

### Task 10: Web curated shelves and shelf pages

**Files:** Modify `src/repohub/web/app.py`, `src/repohub/web/templates/home.html`, `src/repohub/web/templates/_results.html` (if needed); Create `src/repohub/web/templates/_curated.html`, `src/repohub/web/templates/shelf.html`; Test `tests/test_web.py`.

Changes:
- `create_app`: when `shelves is None`, use `load_all_shelves()`; keep `.problems` and show them as banners on the home page (`home.html`). When `shelves` is passed (tests), it is a plain list as today.
- `/shelf/{index}` (home tiles): for a curated shelf call `hub.curated_page(shelf, 0, 6)` and render `_curated.html`; for a search shelf keep today's behaviour.
- New `/shelves/{index}?page=N`: a full shelf page (`shelf.html`). Search shelf: the full result list (same cards as search). Curated shelf: 12 entries per page with "previous / next" links and "showing X-Y of N". `page` must be a positive int within range (otherwise 404); use the parameter name `page` (string then int-parse with a clear 404/422, same care as `_int_param`).
- `_curated.html` card: same card style as `_results.html`, plus the entry's `note` and, when the item is not live, a small dim "as of {{ as_of }}" marker; banners for `errors` like `_results.html` does.
- `home.html`: each shelf heading gets a "See all" link to `/shelves/{i}`.

Tests: home shows the "See all" links and personal-shelf problems; `/shelf/N` for a curated shelf shows notes and the "as of" marker when refresh fails and no marker when live; pagination (page 1 vs 2, first/last links, `page=0`, `page=999`, `page=abc` handled without a 500); notes and snapshot text are escaped (`<script>` in a note is not rendered raw); a search-shelf `/shelves/N` still works; the whole flow uses fake providers.

Commit: `feat(web): curated shelves and full shelf pages`.

---

### Task 11: Terminal curated shelves

**Files:** Modify `src/repohub/tui/app.py`; Test `tests/test_tui.py`.

Changes:
- `RepoHubApp.__init__`: when `shelves is None`, use `load_all_shelves()` and keep `problems`; show a problem count in the status line on the shelf list (`N shelf file problem(s): ...` first problem shown).
- `action_home`: the second column shows `curated · N` for curated shelves (`Text(...)`) and the topic/query otherwise.
- `_open_shelf(index)`: for a curated shelf use `hub.curated_page(shelf, page_offset, PAGE)` with `PAGE = 25`; table columns `Repo, Host, Stars, Lang, Note` (note falls back to the description when empty; every cell a `Text`); status `"{name}: {a}-{b} of {N}  (as of {as_of})"` plus any per-host refresh errors, plain text. Track `self.shelf_index` and `self.shelf_offset`.
- Bindings `]` and `[` move to the next/previous page of a curated shelf (`Binding("]", "next_page", "Next page")`, `Binding("[", "prev_page", "Prev page")`), doing nothing outside a curated shelf view. Selecting a repo row opens the existing detail screen (row keys stay `repo:{host}:{slug}`; skip duplicate keys as `_show_repos` already does).
- Late results must not overwrite the shelf list after Escape (reuse the existing worker-cancel approach) and unexpected exceptions must be caught like the other workers.

Tests (`run_test`, fake providers, tmp shelves): the shelf list shows `curated · 30`; opening a curated shelf shows 25 rows then `]` shows the remaining 5 and `[` returns; hostile note text (`[/]`, `[@click=app.quit]x[/]`) renders literally and the app keeps running; a failing refresh keeps the snapshot rows and shows the error text; Escape returns to the shelf list.

Commit: `feat(tui): curated shelves with paging`.

---

### Task 12: The `repohub` CLI

**Files:** Create `src/repohub/cli.py`, `tests/test_cli.py`; Modify `pyproject.toml` (add `repohub = "repohub.cli:run"` under `[project.scripts]`).

Behaviour (see the design, section 3). `main(argv=None, *, hub_factory=None, stdout=None, stderr=None) -> int` returns the exit code; `run()` does `sys.exit(main())`. `hub_factory` defaults to `repohub.config.build_hub` imported lazily. Subcommands:
- `search TEXT... [--lang L] [--min-stars N] [--days N] [--host github|gitlab|both] [--sort stars|updated|forks] [--no-forks] [--archived] [--limit N (default 20, 1..200)] [--json]`. TEXT (all remaining words joined) goes through `parse_query` with the flags as the base filters; problems go to stderr, one `Ignored: ...` line each.
- `repo HOST:OWNER/NAME [--readme] [--json]`: validate the argument (`host` in `github`/`gitlab`, `valid_slug`) else print an error to stderr and return 2. Uses `hub.detail`.
- `shelves [--json]`: lists shelves from `load_all_shelves()` (index, name, kind `search`/`curated`, entry count for curated). A shelf-file problem is printed to stderr.
- `shelf NAME_OR_INDEX [--limit N] [--json]`: a shelf by exact (case-insensitive) name or numeric index; curated shelves use `curated_page` (first `limit` entries), search shelves use `hub.shelf`; an unknown shelf returns 1.
- `favorites [--json]`: lists the stored favorites (no network).

Output rules:
- `--json` prints exactly one JSON document to stdout (`json.dumps(obj, ensure_ascii=False, indent=2)` plus newline) and nothing else there; everything else (problems, warnings) goes to stderr. Documents: search/shelf/favorites: `{"schema_version": 1, "repos": [Repo.to_dict()...], "errors": {...}, "stale": bool}` (shelf also `"shelf": name`; curated items add `"note"` and `"as_of"` and `"live"` inside each repo object under keys `note`, `as_of`, `live`); repo: `{"schema_version": 1, "repo": {...}, "release": {...} | null, "readme": str | null}` where `readme` is only non-null with `--readme`; shelves: `{"schema_version": 1, "shelves": [{"index": 0, "name": "...", "kind": "search", "entries": 0}]}`.
- Human output is a plain-text table (columns: repo, host, stars, language, updated `YYYY-MM-DD`, description shortened to one line) with widths computed from the data; every cell passes through `clean_text` (single line); `repo` prints a short readable block (name, host, stars/forks/language/license, last push, homepage, topics, description, latest release with each asset and `(arm64)` markers, then the README if requested).
- Exit codes: `0` success; `3` some host failed but results were printed; `1` all hosts failed with no results, or repo/shelf not found, or an unexpected `ProviderError`; `2` usage error (argparse exits with 2 itself).
- Never print tokens; unexpected exceptions print `error: <type>` to stderr without a traceback and return 1.

Tests (`tests/test_cli.py`; call `main([...], hub_factory=lambda: hub, stdout=io.StringIO(), stderr=io.StringIO())` with `make_hub(FakeProvider(...))`):
- `search` JSON: parses as JSON, has `schema_version == 1`, `repos` list with the `Repo` fields (compare keys with `mk().to_dict()`), `errors == {}`, `stale is False`, and stderr is empty; exit 0.
- Flags map to filters via `FakeProvider.last_filters` (lang, min stars, days, host, sort, no-forks, archived) and the query syntax in the text works and its problems go to stderr only; `--limit` truncates.
- Partial failure (one provider raises `ProviderError`) returns 3 with the error in JSON; all providers failing returns 1.
- `repo github:o/r --json` includes the release with assets and their `arch`; `--readme` adds the text, default has `readme: null`; unknown repo returns 1 with a message on stderr; `repo ../x` and `repo nohost:o/r` return 2.
- `shelves --json` lists kinds and counts; `shelf` by name and by index; unknown shelf returns 1; curated shelf JSON includes `note`, `as_of`, `live`.
- `favorites --json` lists stored favorites and makes no provider calls.
- Human output: contains the repo slug and no ANSI/control characters even when the fake data has them; JSON mode never mixes text into stdout.
- No token appears in any output (put a fake token string into the environment and assert it is absent).
- `python -m repohub.cli`-style invocation is NOT tested (no network); `repohub --help` (`main(["--help"])`) raises `SystemExit(0)` without calling `hub_factory` (make the factory raise if called).

Commit: `feat: add the scriptable repohub CLI`.

---

### Task 13: Docs, version and final verification

**Files:** Modify `README.md`, `docs/plans/2026-09-21-phase1-design.md` (status line + a short "Changes made during implementation" section), `src/repohub/__init__.py` and `pyproject.toml` (version `0.2.0`), `tests/test_smoke.py` (version assertion).

Steps:
1. README: add a "Search syntax" section (the tokens, examples, that the web search box and the terminal app share it, unknown tokens stay as words, bad values are ignored with a message); a "Shelves" section (packaged defaults, the 8 catalog shelves with the 2026-09-20 snapshot and lazy refresh, the personal file `~/.config/repohub/shelves.yaml` with a short example of a search shelf and a curated shelf, how errors are reported); a "Command line" section (subcommands, flags, `--json` document shapes, exit codes); the terminal keys `[` and `]`; remove the now-false limitation "edit installed copy" for shelves; keep every other statement accurate to the code. Verify each documented command actually runs (for example `.venv/bin/repohub --help` and `.venv/bin/repohub shelves` may call `load_all_shelves()` only, which reads the config dir: run it with `XDG_CONFIG_HOME` pointed at a temp dir; do not call anything that needs the network).
2. Design doc: change the status line to say Phase 1 is implemented, and append the deviations from the design that actually happened (only what is in the code).
3. Bump the version to `0.2.0` in `src/repohub/__init__.py` and `pyproject.toml` and update the smoke test.
4. Final verification, reported plainly: `.venv/bin/pytest -q` (count); `uv build --wheel --out-dir <fresh temp dir outside the repo>` and list the wheel: it must contain `repohub/core/shelves.yaml`, `repohub/core/catalog_shelves.yaml`, `repohub/cli.py`, the web templates and static files; delete the temp dir; `git status --short` clean; `git log --oneline | head`. Do not start servers or the TUI.

Commits (separate): `docs: document search syntax, shelves and the CLI` and `chore: bump version to 0.2.0`.

---

## Risks to watch during execution

- Textual 8.x API details in Task 11 (`Binding` on `[`/`]`, `DataTable` cursor behaviour): adapt the smallest thing and keep assertions.
- Task 7 must keep every existing `tests/test_browse.py` test passing while extending the loader.
- Task 8's concurrency test needs a provider that records peak concurrent `repo()` calls; keep it deterministic (use `asyncio.Event`/`asyncio.sleep(0)`, no real sleeps).
- Curated refresh adds API calls (bounded to one page at a time and cached one hour); without a token GitHub allows roughly 10 search requests a minute but `GET /repos/...` is a different, larger bucket. If a rate limit shows up in manual use, the snapshot is the fallback by design.
