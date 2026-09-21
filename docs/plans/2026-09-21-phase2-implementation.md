# RepoHub Phase 2 Implementation Plan: Other hosts

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (or executing-plans) to implement this plan task-by-task.

**Goal:** Replace the hard-coded GitHub/GitLab pair with a host registry, add a Forgejo/Gitea provider with Codeberg built in, and let the user add more Forgejo/Gitea instances in a config file.

**Architecture:** A new `repohub.core.hosts` module holds `HostSpec`, `HostRegistry` and a process-wide active registry. Every place that listed hosts reads the registry. A new `ForgejoProvider` implements the same provider interface as the GitHub and GitLab providers. `config.build_hub()` builds one provider per registered host. Design: `docs/plans/2026-09-21-hosts-accounts-design.md` (section "Phase 2: other hosts").

**Tech Stack:** unchanged (Python 3.11+, httpx, FastAPI/Jinja/htmx, Textual, SQLite, PyYAML, platformdirs, pytest, pytest-asyncio, respx).

---

## Conventions (apply to every task)

- Work in `/home/daripper/Projects/repohub` on branch `main`. Use the project venv: `.venv/bin/pytest`, `.venv/bin/python`. Machine: Fedora Asahi aarch64, 7 GB RAM; keep everything light.
- TDD: write failing tests first, run them and confirm they fail for the right reason, implement the minimum, run the whole suite (`.venv/bin/pytest -q`; baseline before Task 1 is **522 passed, 2 deselected**), commit.
- Every commit ends with the trailer, using two `-m` flags: `git commit -m "feat: ..." -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"`.
- No test touches the real network, real git, real tokens, or the real user data or config directories. Never call `repohub.config.build_hub()` in a test unless the test monkeypatches `XDG_CONFIG_HOME`/`XDG_DATA_HOME` to tmp dirs AND replaces the providers (prefer testing pieces directly). Never start a server or the real TUI in a test (use Textual's `run_test`, FastAPI's `TestClient(app, base_url="http://localhost")`). Do not run any command that needs the network except where a task explicitly says so.
- Untrusted text (API data, YAML notes, README text) is never interpreted as markup. Terminal cells are `rich.text.Text(...)`; web templates rely on autoescape; provider data goes through `clean_text` (`src/repohub/core/textsafe.py`) and URLs through `safe_url`.
- Tokens: never log, print, store or put them in exceptions, output or cache keys. A token is sent only to the host it belongs to.
- Do not weaken the clone protections (`src/repohub/core/clone.py`): extending them to more hosts changes only the allow-list. Do not push to any remote.
- Do not weaken an existing test. Tests that assert the old default of two hosts (`("github", "gitlab")`) legitimately change to the new default (all registered hosts); say so in your report. If a plan test contains a genuine bug, fix it minimally and say so.
- The registry is process-wide state. `tests/conftest.py` (created in Task 1) resets it after every test so tests cannot leak a custom registry into each other.
- Existing helpers: `tests/helpers.py` has `mk(...)`, `FakeProvider`, `make_hub(*providers, clock=None)`.

---

### Task 1: Host registry

**Files:** Create `src/repohub/core/hosts.py`, `tests/conftest.py`, `tests/test_hosts.py`.

Full code for `src/repohub/core/hosts.py` (extend only if a test needs it):

```python
from __future__ import annotations

import re
from dataclasses import dataclass, field

KINDS = ("github", "gitlab", "forgejo")
ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,19}$")


@dataclass(frozen=True)
class HostSpec:
    id: str
    kind: str
    name: str
    domain: str
    api_base: str
    token_env: tuple[str, ...] = ()
    builtin: bool = False

    @property
    def web_base(self) -> str:
        return f"https://{self.domain}"


BUILTIN_HOSTS = (
    HostSpec("github", "github", "GitHub", "github.com", "https://api.github.com", ("GITHUB_TOKEN", "GH_TOKEN"), True),
    HostSpec("gitlab", "gitlab", "GitLab", "gitlab.com", "https://gitlab.com/api/v4", ("GITLAB_TOKEN",), True),
    HostSpec("codeberg", "forgejo", "Codeberg", "codeberg.org", "https://codeberg.org/api/v1", ("CODEBERG_TOKEN",), True),
)


class HostRegistry:
    """An ordered set of hosts. The first host wins ties when results are merged."""

    def __init__(self, specs):
        self._specs: dict[str, HostSpec] = {}
        self._domains: dict[str, str] = {}
        for spec in specs:
            if spec.kind not in KINDS:
                raise ValueError(f"unknown host kind {spec.kind!r}")
            if not ID_RE.match(spec.id):
                raise ValueError(f"invalid host id {spec.id!r}")
            if spec.id in self._specs:
                raise ValueError(f"duplicate host id {spec.id!r}")
            if spec.domain in self._domains:
                raise ValueError(f"duplicate host domain {spec.domain!r}")
            self._specs[spec.id] = spec
            self._domains[spec.domain] = spec.id

    @property
    def specs(self) -> tuple[HostSpec, ...]:
        return tuple(self._specs.values())

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(self._specs)

    def __contains__(self, host_id: object) -> bool:
        return host_id in self._specs

    def get(self, host_id: str) -> HostSpec | None:
        return self._specs.get(host_id)

    def rank(self, host_id: str) -> int:
        """Position in the registry (used to break ties); unknown hosts sort last."""
        return self.ids.index(host_id) if host_id in self._specs else len(self._specs)

    def id_for_domain(self, domain: str) -> str | None:
        return self._domains.get(domain.lower())

    def clone_domains(self) -> dict[str, str]:
        return {s.id: s.domain for s in self._specs.values()}

    def slug_ok(self, host_id: str, slug: str) -> bool:
        """Slug shape for a configured host: owner/name for github and forgejo, nested groups for gitlab."""
        from repohub.core.providers.base import valid_slug

        spec = self._specs.get(host_id)
        if spec is None:
            return False
        return valid_slug(slug, "gitlab" if spec.kind == "gitlab" else "github")


_active = HostRegistry(BUILTIN_HOSTS)


def registry() -> HostRegistry:
    return _active


def set_registry(reg: HostRegistry) -> None:
    global _active
    _active = reg


def reset_registry() -> None:
    set_registry(HostRegistry(BUILTIN_HOSTS))
```

`tests/conftest.py`:

```python
import pytest

from repohub.core import hosts


@pytest.fixture(autouse=True)
def _reset_host_registry():
    hosts.reset_registry()
    yield
    hosts.reset_registry()
```

Tests (`tests/test_hosts.py`), all pure:
- Built-in ids are exactly `("github", "gitlab", "codeberg")` in that order; kinds are `github`, `gitlab`, `forgejo`; codeberg's `api_base` is `https://codeberg.org/api/v1` and `web_base` is `https://codeberg.org`; every built-in has `builtin=True`.
- `__contains__`, `get` (unknown gives `None`), `rank` (github 0, gitlab 1, codeberg 2, unknown last), `id_for_domain` (case-insensitive; `evil.example` gives `None`), `clone_domains()` mapping.
- `slug_ok`: `github`/`codeberg` need exactly `owner/name` (`o/r` yes, `o/r/x` no, `o/../x` no, `o/r\n` no); `gitlab` accepts `g/sub/p`; an unknown host id is False.
- `HostRegistry` rejects an unknown kind, an invalid id (`Bad`, `1x`, too long, empty), a duplicate id, and a duplicate domain, each with `ValueError`.
- `set_registry`/`registry`/`reset_registry` work, and a second test proves the autouse fixture reset the registry (a test that sets a custom registry, followed by one asserting the built-ins are back; order-independent: do it inside one test with try/finally plus a separate test that only asserts built-ins).

Commit: `feat: add the host registry`.

---

### Task 2: The `hosts.yaml` loader

**Files:** Create `src/repohub/core/hostsconfig.py`, `tests/test_hostsconfig.py`.

Behaviour:
- `personal_hosts_path() -> Path`: `Path(user_config_dir("repohub")) / "hosts.yaml"`.
- `load_hosts(path=None) -> LoadedHosts` where `LoadedHosts` is a dataclass with `registry: HostRegistry` and `problems: list[str]`. `path=None` means the personal path. A missing file is silent and gives the built-in registry. Extra hosts are appended after the built-ins.
- `configure_hosts(path=None) -> list[str]`: calls `load_hosts`, calls `set_registry(loaded.registry)`, returns the problems.
- File rules: regular files only (stat first, like the shelves loader; a FIFO, directory, socket or device is a problem, never opened), at most 256 KB, UTF-8, `yaml.safe_load` only; the top level must be a list (empty file or `null` means none); at most 20 entries. A file-level failure adds one problem `hosts file (<path>): <message>` (path cleaned with `clean_text` and capped at 200 characters) and the file is skipped; the built-ins always load.
- Entry rules (an invalid entry adds a problem `hosts file entry #N: <message>` (0-based) and only that entry is skipped): must be a mapping with only the keys `id`, `kind`, `url`, `token_env`, `name` (unknown keys are errors); `id` (required) matches `ID_RE` and is not a built-in id and not already used; `kind` (required) must be `forgejo` (message: `only kind 'forgejo' is supported for extra hosts`); `url` (required) must be a string that parses as `https` with a plain ASCII hostname (letters, digits, `-` and `.` only; each label 1 to 63 characters, no leading or trailing hyphen or dot; total at most 253), NO userinfo, NO port, NO query, NO fragment, NO params, and the path must be empty or `/`; the hostname is lowercased and becomes `domain`, and `api_base` is `https://<domain>/api/v1`; the domain must not equal a built-in domain or a previously loaded extra domain; `name` (optional) is a string, cleaned with `clean_text`, at most 40 characters, default the id; `token_env` (optional) must match `^REPOHUB_[A-Z][A-Z0-9_]{0,50}_TOKEN$` (changed after the Task 2 security review: extra hosts may only read `REPOHUB_*_TOKEN` variables) and must not equal ANY token variable already used by another host (built-in or extra), which prevents sending one host's token to another; the default is `REPOHUB_<ID upper-cased with '-' replaced by '_'>_TOKEN` and that default must also be unique (otherwise a problem).
- Every error message that echoes untrusted text uses a helper that runs `clean_text` and caps at 60 characters.

Tests (tmp dirs only; never the real config dir; add one test that `personal_hosts_path()` honours `XDG_CONFIG_HOME` via monkeypatch):
- Missing file gives built-ins and no problems; a valid file with two hosts appends them in order with the right `domain`, `api_base`, `token_env` defaults and `name`; `load_hosts` does not change the active registry but `configure_hosts` does (and returns the problems).
- Every entry rule: bad id (uppercase, too long, built-in id `github`, duplicate), wrong kind (`github`, `gitlab`, missing), missing url, `http://`, userinfo (`https://u:p@x.org`), port (`https://x.org:3000`), path (`https://x.org/git`), query/fragment, non-ASCII/IDN host, hostname with underscore/space/leading hyphen/double dot/too long label, an IP literal is accepted only if it matches the hostname rule (decide: reject IP literals and `localhost` with a clear message - state your decision), domain equal to `github.com`/`codeberg.org` or repeated, unknown keys, non-mapping entries, `token_env` not ending in `_TOKEN`, lowercase, equal to `GITHUB_TOKEN`/`CODEBERG_TOKEN` or another extra's variable, default token name collisions.
- An invalid entry does not stop later valid entries; problems are numbered 0-based.
- File-level: a directory, a FIFO (use `os.mkfifo` with a thread-join timeout guard so the test fails fast instead of hanging), a symlink to a regular file (works), a file over 256 KB, invalid UTF-8, a top-level dict, more than 20 entries, `!!python/object` tags, deeply nested YAML (`[` repeated 100000 times), and a YAML alias bomb all produce a problem (or load fine when harmless) and never raise or hang; built-ins are always present.
- Messages never contain raw control characters or escape sequences from hostile input (assert on a hostile `id`/`url`).

Commit: `feat: load extra hosts from hosts.yaml`.

---

### Task 3: Read the registry everywhere the host list was hard-coded

**Files:** Modify `src/repohub/core/models.py`, `src/repohub/core/queryparse.py`, `src/repohub/core/browse.py`, `src/repohub/core/search.py`, `src/repohub/core/clone.py`, `src/repohub/core/providers/base.py` (only if needed); update the affected tests.

Changes:
- `models.py`: remove the `HOSTS` constant. `SearchFilters.hosts` becomes `hosts: tuple[str, ...] = field(default_factory=lambda: registry().ids)`. (Frozen dataclass default factories are fine; `asdict` and the cache key keep working.) Anything that imported `HOSTS` must be updated (web, CLI, queryparse, browse): grep for it.
- `queryparse.py`: `host:` accepts any registered id (case-insensitive) and `both` or `all` (all registered hosts); the error becomes `host must be one of: github, gitlab, codeberg, all` built from the registry (echo only registry ids). A `host:` value that names a configured host restricts to it.
- `browse.py`: a shelf entry's host must be syntactically valid (`ID_RE`), NOT necessarily configured: an entry for a host that is not configured is kept and treated as unavailable later (Task 8). For a configured host the slug is checked with `registry().slug_ok(host, slug)`; for an unconfigured host use the lenient rule `valid_slug(slug, "gitlab")` (owner/name or deeper, safe characters). All existing shelf tests must keep passing (entries that used `bitbucket:o/r` as an "unknown host" error example now need a syntactically invalid host such as `Bad_Host:o/r`; update those tests and say so).
- `search.py`: the tie-break and the "which duplicate survives" rule use `registry().rank(host)` (lower rank wins ties) instead of `host == "github"`; behaviour for github/gitlab stays identical.
- `clone.py`: the host mapping comes from `registry().clone_domains()` at call time. `clone_url(host, slug)` requires a configured host and `registry().slug_ok`; the URL host check accepts any configured domain; the host id is found with `id_for_domain`. The userinfo, port, query, whitespace, `..`, canonical-URL and lock-down logic is unchanged. Error messages say `only plain https URLs on a configured host are allowed` (update the tests that matched the old text; keep asserting `CloneError`). Add tests: a Codeberg URL clones (fake runner) to the right folder with the canonical URL `https://codeberg.org/o/r.git`; a custom registry with an extra host (set with `set_registry`) is accepted and an unconfigured domain is rejected; every old rejection case still fails (userinfo, port, http, look-alike domains such as `codeberg.org.evil.com` and `github.com@evil.com`).
- Update tests that assert the old two-host defaults so they expect all registered hosts: for example `SearchFilters().hosts == ("github", "gitlab", "codeberg")`, `host:both` restoring all three, search tests where a provider dict lacks `codeberg` (those keep working because `search_all` only queries hosts that have a provider).

New tests: `host:codeberg` restricts to codeberg; `host:ALL`; `host:nowhere` gives the problem listing registry ids; a custom registry (via `set_registry`) changes what `host:` accepts and the default `hosts`; merge tie-break with three hosts (equal stars: github, then gitlab, then codeberg); dedupe keeps the higher-ranked host on ties.

Commit: `refactor: read hosts from the registry instead of hard-coded pairs`.

---

### Task 4: The Forgejo/Gitea provider

**Files:** Create `src/repohub/core/providers/forgejo.py`, `tests/test_forgejo.py`.

`ForgejoProvider(host_id: str, base_url: str, token: str | None = None)` (`host = host_id`) mirrors `GitHubProvider`/`GitLabProvider`: read `providers/github.py` and `gitlab.py` first and reuse their patterns (`guard_parse`, `_get` with rate-limit/401/404/redirect handling, `token_rejected` fallback that drops the header once and retries anonymously, `NotFound`, `clean_text`, `safe_url`, `aclose`). Differences and facts (verified against codeberg.org on 2026-09-21):
- Auth header: `Authorization: token <t>` only when a token exists. `User-Agent: repohub`, timeout 15 s, redirects NOT followed, status >= 300 is a `ProviderError`. `429` (and a `Retry-After` header) is a `RateLimited` (use the GitLab provider's Retry-After handling); `401` is "token rejected" with the anonymous retry.
- `search(query, filters, per_page=30)`: `GET /repos/search` with params `sort` (`stars` for `stars` and `forks`, `updated` for `updated`), `order=desc`, `limit` (at most 50), `mode=source` when `hide_forks`, `archived=false` unless `include_archived`. Response is `{"ok": true, "data": [...]}` (missing `data` is a malformed response, `ProviderError` via `guard_parse`). Text query goes in `q`. Topics: when `filters.topic` is set and there is no text query, send `q=<topic>` with `topic=true`; when there is both, send the text in `q` and filter client-side to repos whose `topics` contain the topic. Language, minimum stars and recency are NOT sent (the endpoint does not filter on them): apply the language filter inside the provider (case-insensitive equality) since `search_all` does not; `search_all._keep` already applies stars, archived, fork and recency.
- Repo mapping: `slug=full_name`, `url=safe_url(html_url)` (fallback `https://<domain>/<slug>` using the base URL's hostname), `description`, `stars=stars_count`, `forks=forks_count`, `language`, `license=""` (unknown: the server does not expose it), `topics`, `archived`, `fork`, `homepage=safe_url(website)`, and `pushed_at` from `updated_at` normalised to UTC `YYYY-MM-DDTHH:MM:SSZ` (parse with `datetime.fromisoformat`; a value that fails to parse becomes `""`).
- `repo(slug)`: `GET /repos/{owner}/{repo}` (404 gives `NotFound`); slugs validated with `valid_slug(slug, "github")` (exactly two segments) BEFORE any request; the path is built only from validated parts.
- `readme(slug)`: try `GET /repos/{o}/{r}/raw/{name}` for `README.md`, `README.markdown`, `README.rst`, `README.txt`, `README` (names are case-sensitive on the server; 404 moves on, any other error propagates), text cleaned by the caller as today.
- `latest_release(slug)`: `GET /repos/{o}/{r}/releases/latest` (404 gives `None`): `tag=tag_name`, `published_at`, assets from `assets[]` with `name`, `size`, `browser_download_url` (through `safe_url`, an asset without a safe URL is skipped) and `arch=parse_arch(name)`.
- Use the real Codeberg JSON shape for fixtures (a search result item has the keys `full_name`, `html_url`, `description`, `stars_count`, `forks_count`, `language`, `topics`, `fork`, `archived`, `updated_at` like `2026-09-18T13:02:54+02:00`, `website`, plus many others; a release has `tag_name`, `published_at` and `assets` entries with `name`, `size`, `browser_download_url`, `type`, `uuid`).

Tests (respx; base URL `https://codeberg.org/api/v1`; also one test with a custom base URL for an extra instance): search params and mapping (including the timezone normalisation from `+02:00` to `Z`), `mode=source` only with hide_forks, `archived=false` default, sort mapping, topic-only query uses `topic=true`, topic plus text filters client-side, language filter, `{"ok": true}` without `data` and non-JSON body give `ProviderError`, 429 with `Retry-After` gives `RateLimited`, 401 with token retries anonymously once (header dropped, `token_rejected` True) and a second 401 raises, 3xx is an error, network error is `ProviderError("network error")`, token header only when set and never sent elsewhere, slug validation (`../x`, `o/r/x`, `o/r\n` rejected before any request; assert respx saw no call), README candidate order and 404 fall-through, release mapping with arm64 detection, no release gives `None`, hostile description/topic control characters are cleaned, `javascript:` website and html_url are neutralised, unknown fields ignored, missing optional fields tolerated (no `topics`, no `language`, no `website`).

Commit: `feat: add the Forgejo/Gitea provider`.

---

### Task 5: Build providers and tokens from the registry

**Files:** Modify `src/repohub/config.py`, `src/repohub/core/auth.py`, `src/repohub/core/hub.py` (only to carry problems); Test `tests/test_auth.py`, `tests/test_config.py` (new).

Changes:
- `auth.py`: keep the existing `Tokens`, `find_tokens` and their tests unchanged. Add `find_host_tokens(reg=None, env=None, gh_cli=_gh_cli_token) -> dict[str, str | None]`: for each registered host the first non-empty (stripped) value among its `token_env` variables; for the `github` host only, fall back to the GitHub CLI when no env value is found (same helper as today, never called for other hosts). Never echo values; the returned mapping is not `repr`-safe, so wrap it in a small frozen dataclass `HostTokens` whose `repr`/`str` never show values, with `for_host(host_id)`.
- `config.py`: `build_hub()` (a) calls `configure_hosts()` and keeps the returned problems, (b) creates a provider for every registered host by kind (`GitHubProvider(token)`, `GitLabProvider(token)`, `ForgejoProvider(spec.id, spec.api_base, token)`) with that host's own token, (c) returns the Hub with a new attribute `hub.host_problems: list[str]`. `Hub.__init__` gets an optional keyword `host_problems=None` (default empty list) so front ends can show them. `build_hub` is still never called from tests: test a new pure helper `make_providers(reg, tokens) -> dict` and `HostTokens` instead, using a custom registry.
- The token for one host must never be given to another host's provider (test with two extra hosts and distinct fake values).

Tests: `find_host_tokens` per host from a fake env (`CODEBERG_TOKEN`, an extra host's `REPOHUB_MYFORGE_TOKEN`), whitespace stripping, empty values ignored, GitHub CLI fallback only for github (fake gh_cli raising if called for others), `repr` never shows a value, `make_providers` returns the right classes and base URLs (for extra hosts the `api_base` from the spec) and hands each provider only its own token (inspect the private client headers in the test only), an unknown kind cannot occur.

Commit: `feat: build providers and tokens for every registered host`.

---

### Task 6: Web app

**Files:** Modify `src/repohub/web/app.py`, `src/repohub/web/templates/base.html`, `src/repohub/web/templates/home.html` (host problems banner), `src/repohub/web/static/app.css`; Test `tests/test_web.py`.

Changes:
- Replace remaining `HOSTS` uses and literal host lists with the registry: `require_repo` uses `registry().slug_ok(host, slug)` (unknown host gives 404); `/search` accepts `host=both|all` or a registered id (400 otherwise); the dropdown in `base.html` is built from a `host_ids` value that `page()` adds to every template context (`["all"] + registry ids`, `both` still accepted in URLs for old links). The `/repo/{host}/{slug}` and favorites/clone routes accept `codeberg` and configured extras.
- Host problems (`hub.host_problems`) are shown as banners on the home page like the shelf-file problems (autoescaped, capped at 10).
- CSS: `.badge.codeberg` gets its own colour; other ids fall back to the default badge style. Host ids are validated (`ID_RE`) and go into class attributes only through autoescape.
- Clone confirmation and the favorites/clone POST routes work for any registered host (the existing safety checks stay).

Tests: `/search?host=codeberg` reaches only the codeberg provider (`FakeProvider("codeberg", ...)` via `make_hub`); `host=nowhere` is 400; `host=all` and legacy `host=both` query every provider; the dropdown lists all registered hosts (custom registry via `set_registry`); `/repo/codeberg/o/r` renders a Codeberg repo with its badge; an unregistered host in `/repo/...` is 404; favorites toggle and clone confirm work for codeberg (fake cloner); host problems appear as escaped banners (hostile text).

Commit: `feat(web): support every registered host`.

---

### Task 7: Terminal app and CLI

**Files:** Modify `src/repohub/tui/app.py`, `src/repohub/cli.py`; Test `tests/test_tui.py`, `tests/test_cli.py`.

Changes:
- TUI: the host column already shows the id as text; make sure detail, favorites, clone and shelf rows work for `codeberg` and extra hosts (the validation now goes through the registry); host problems from the Hub are shown in the status line on the shelf list next to the shelf problems (plain text, `markup=False`, capped).
- CLI: `--host` choices are built from the registry at parser-build time (`both`, `all` and each registered id); `repo HOST:OWNER/NAME` validates with the registry (unregistered host or bad slug exits 2 before any Hub call); default hosts follow `SearchFilters()`. New read-only subcommand `repohub hosts [--json]` listing configured hosts (`id`, `kind`, `name`, `domain`, `builtin`, and whether a token is present as `token: true/false`, NEVER the token or its variable value) and printing hosts-file problems to stderr; it must not need the network or build the full Hub (call `configure_hosts()` directly). JSON: `{"schema_version": 1, "hosts": [...], "problems": [...]}`.
- The CLI's `configure_hosts()` call happens after argparse (so `--help` stays free) and before the parser's `--host` choices are needed: build the parser lazily after configuring hosts when a subcommand needs them, keeping `--help`/`--version`/usage errors free of file reads if practical; if that is not practical, configuring hosts at parser build time is acceptable because it only reads the small hosts file (state your decision and keep the no-hub, no-network guarantee).

Tests: TUI opening a codeberg repo detail from a search result (fake provider) and favoriting it; host problems shown as plain text (hostile text does not crash or quit); CLI `--host codeberg` maps to filters, `--host nowhere` exits 2, `repo codeberg:o/r --json` works with a fake hub, `repo nowhere:o/r` exits 2, `hosts --json` output shape (built-ins present, `builtin: true`, tokens shown only as booleans, a fake token value from the environment never appears in stdout or stderr), extra host from a tmp `XDG_CONFIG_HOME` hosts file appears with `builtin: false`, a broken hosts file gives a stderr problem line and still lists the built-ins.

Commit: `feat: TUI and CLI support for every registered host`.

---

### Task 8: A Codeberg shelf and unavailable hosts

**Files:** Move `docs/plans/phase2-codeberg-shelf.yaml` to `src/repohub/core/catalog_codeberg.yaml` with `git mv` (contents untouched: one curated shelf, `Catalog: Codeberg`, 18 entries, `as_of: '2026-09-21'`); Modify `src/repohub/core/browse.py` (load it after the catalog), `src/repohub/core/hub.py` (unavailable hosts); Tests `tests/test_catalog.py` (keep the existing 8-shelf/171-entry assertions valid: the new file is separate, so update ordering expectations only), `tests/test_browse.py`, `tests/test_hub.py`.

Changes:
- `load_all_shelves`: packaged files in this order: `shelves.yaml`, `catalog_shelves.yaml`, `catalog_codeberg.yaml`, then the personal file. A missing packaged optional file is not an error; a broken packaged file still raises.
- Hub: a curated entry whose host is not configured never calls a provider: `curated_page` keeps its snapshot with `live=False` and records the error `<host>: host not configured` (one message per host id, sanitised); `repo_summary` for an unregistered host raises `ProviderError(host, "host not configured")` (change the old `unknown host` wording and update tests). Favorites for unregistered hosts stay stored; `refresh_favorites` skips them (already the case).
- Search-side: hosts without providers are skipped by `search_all` (already the case).

Tests: the packaged Codeberg shelf loads (18 entries, all `codeberg:`), valid slugs, notes non-empty, no control characters, `as_of` set; `load_all_shelves` order (defaults, catalog, codeberg, personal); a personal shelf entry for an unconfigured host loads and renders as unavailable with the snapshot and the error text (web page banner, CLI `live: false`, TUI status); a curated page with mixed configured/unconfigured hosts only calls the configured provider; removing a host from the registry does not delete favorites.

Commit: `feat: add a Codeberg catalog shelf and handle unconfigured hosts`.

---

### Task 9: Opt-in live tests for Codeberg

**Files:** Modify `tests/test_live.py`.

Add opt-in (`@pytest.mark.live`, excluded by default) tests against the real Codeberg API through `ForgejoProvider("codeberg", "https://codeberg.org/api/v1")` with no token: a search (`terminal`, sorted by stars) returns repos with `host == "codeberg"`; `repo("dnkl/foot")` returns stars above 100; `readme("dnkl/foot")` returns text; `latest_release("dnkl/foot")` returns a release or `None` without raising. Tolerate rate limits and network failures by `pytest.skip` with a clear reason (a live test must not fail the suite because the network is down). Run `.venv/bin/pytest -m live -k codeberg -v` ONCE (this is the only network use in this phase besides Task 10's manual check) and report the result plainly.

Commit: `test: add opt-in live tests for Codeberg`.

---

### Task 10: Docs, version and final verification

**Files:** Modify `README.md`, `SECURITY.md`, `docs/plans/2026-09-21-hosts-accounts-design.md` (status plus an implementation-changes section), `src/repohub/__init__.py`, `pyproject.toml`, `tests/test_smoke.py`.

Steps:
1. README: update the feature table and intro (three hosts; Codeberg built in), a new "Hosts" section (built-in hosts, the personal `hosts.yaml` with an example entry `- {id: myforge, kind: forgejo, url: https://git.example.org, token_env: REPOHUB_MYFORGE_TOKEN}`, every rule and limit from Task 2 in plain words, how problems are shown, tokens: `CODEBERG_TOKEN` and each extra host's `token_env`, license shown as unknown on Forgejo hosts, clone allowed only from configured hosts), update the search syntax (`host:codeberg`, `all`), the CLI docs (`--host`, `repohub hosts`, the JSON shape), the roadmap (Phase 2 done with its ten tasks, Phase 3 next) and Known limitations (dedupe by owner/name across three hosts, Forgejo search cannot filter by language/stars/recency server-side so pages can shrink, Forgejo servers vary by version, Bitbucket not planned). Re-check every statement against the code; verify any offline command you document.
2. SECURITY.md: add the hosts file and the Forgejo provider to the in-scope list (URL validation, token isolation between hosts, redirects not followed, clone allow-list).
3. Design doc: mark Phase 2 implemented and append a factual "Changes made during Phase 2 implementation" section.
4. Version `0.3.0` (`src/repohub/__init__.py`, `pyproject.toml`, `tests/test_smoke.py`).
5. Final verification, reported plainly: `.venv/bin/pytest -q` (count); wheel build in a fresh temp dir outside the repo (`uv build --wheel --out-dir <tmp>`) and a listing that includes `repohub/core/hosts.py`, `hostsconfig.py`, `providers/forgejo.py` and `core/catalog_codeberg.yaml`; delete the temp dir; `git status --short` clean; `git log --oneline | head`.

Commits (separate): `docs: document hosts, Codeberg and the hosts file` and `chore: bump version to 0.3.0`.

---

## Risks to watch during execution

- The global registry is shared state: every test that changes it must restore it (the autouse fixture in `tests/conftest.py` does this), and production code must call `configure_hosts()` exactly once at startup (in `build_hub()`).
- Task 3 touches many modules and tests at once; keep the change mechanical (no new behaviour beyond the registry) and run the whole suite after each file.
- A misconfigured `token_env` could leak one host's token to another; Task 2's uniqueness and `_TOKEN`-suffix rules and Task 5's per-host token isolation tests exist to prevent that.
- Forgejo servers vary by version; tolerate missing fields and never assume licenses, topics or websites exist.
