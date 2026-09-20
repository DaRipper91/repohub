# RepoHub Design

Date: 2026-09-20
Status: Approved design, not yet implemented

RepoHub is a "store front" for source repositories. Users search and browse GitHub and GitLab from one place, open a store-style detail page for any repo, save favorites and clone repos. It has two front ends, a web UI and a terminal UI, on top of one shared core.

## Decisions

| Question | Decision |
|---|---|
| Platform | Web app and terminal app (TUI) |
| Stack | Python, one package |
| V1 features | Cross-host search, repo detail page, browse shelves, favorites and clone |
| API access | Optional tokens, auto-detected: `GITHUB_TOKEN` / `GITLAB_TOKEN`, then `gh auth token`, else anonymous |
| Structure | Approach A: shared core; FastAPI + Jinja + htmx web UI; Textual TUI importing the core directly |
| Location | `~/Projects/repohub` |

Rejected: (B) TUI as an HTTP client of the web server, which adds a required process and latency; (C) a single-page app, which adds a Node build chain and a second UI stack.

## Target machine

Fedora Asahi Linux, aarch64, 7.3 GB shared RAM. Memory use matters, so there is no JavaScript build step and the TUI runs without the web server.

## Architecture

```
repohub/
  core/
    models.py      Repo, Release, Asset: one normalized shape for both hosts
    providers/     github.py, gitlab.py: each returns Repo objects
    search.py      fan out to both providers, merge, rank, dedupe
    browse.py      shelves: named queries (Trending, Terminal Tools, Local AI...)
    auth.py        finds tokens: env vars, then `gh auth token`, else anonymous
    cache.py       SQLite response cache with TTL, protects rate limits
    store.py       favorites, stored in the same SQLite file
    clone.py       git clone into a chosen folder
  web/             FastAPI app, Jinja templates, htmx, static CSS
  tui/             Textual app
```

- Providers hide host differences (GitHub `stargazers_count` vs GitLab `star_count`, license fields, release assets). Front ends only see `Repo`.
- Tokens are read at startup, held in memory, never logged and never written to disk.
- The web server binds to `127.0.0.1` by default.
- One `pyproject.toml`, two entry points: `repohub-web` and `repohub-tui`.

## Data model

```
Repo:     host, slug, url, description, stars, language, license,
          topics, pushed_at, archived, forks, homepage
Release:  tag, published_at, assets[]
Asset:    name, size, url, arch   (arch parsed from filename:
          arm64/aarch64, x86_64, or unknown)
```

Releases are fetched on demand for the detail page, not during search, so a search costs one API call per host.

## Search

1. Input: text plus optional filters: language, minimum stars, updated within N days, host (both, GitHub only, GitLab only).
2. `search.py` queries both providers concurrently.
   - GitHub: repository search with `stars:>=N`, `language:` and `pushed:>` qualifiers.
   - GitLab: projects endpoint with `search=`, ordered by stars. Filters it cannot express are applied client-side.
3. Merge, sort by stars by default, and dedupe by normalized slug (GitLab mirrors of GitHub projects are common).
4. Archived repos are hidden unless a filter enables them.
5. If one host fails or is rate limited, the other host's results still show, with a banner naming the failed host.

## Browse shelves

- Shelves are named queries in a plain YAML file, editable without code changes. Examples: Terminal tools (topics `tui`, `cli`), Local AI (topic `llm`), Retro and emulation, Self-hosted.
- Each shelf has a star floor and an activity window to keep results fresh.
- The home screen shows a few results per shelf. Selecting a shelf opens the full list.

## Cache

- Search results: 10 minutes. Repo details: 1 hour.
- Key includes host, query and filters.

## Repo detail page

- Header: name, host badge, stars, language, license, last push, "archived" warning if applicable.
- Body: the README from the default branch. Web UI: Markdown rendered then sanitized with `nh3` (READMEs are untrusted). TUI: Rich Markdown.
- Sidebar: topics, homepage, latest release and its assets. Each asset shows its parsed architecture. A green "arm64" badge appears when one exists. If a repo has no releases, say so.
- Actions: Favorite, Clone.

## Favorites and clone

- Favorites live in the same SQLite file, keyed by host and slug. The Favorites view reads local data and refreshes stats in the background at most once a day.
- Clone shows the exact destination path and asks for confirmation. Default folder `~/playground`, configurable.
  - Runs `git clone` with an argument list, never through a shell.
  - Accepts only URLs on `github.com` or `gitlab.com`.
  - Refuses to overwrite an existing folder.
  - Verifies the resolved path stays inside the chosen directory.
  - Never runs install scripts or build steps after cloning.
- Web protection: localhost binding plus a per-session token required on state-changing requests (favorite, clone), so an unrelated browser page cannot trigger them.

## Error handling

- Rate limits (403/429): show the reset time and serve cached results when available.
- Network failure: banner, keep cached data.
- Rejected token: fall back to anonymous for that host and say so, without echoing the token.
- Logs never contain tokens or full request headers.

## Testing

- Providers and search: `pytest` with mocked HTTP (`respx`) and saved API response fixtures. No unit test touches the network. Covers normalization, merge and dedupe, and partial failure.
- Arch parsing: table tests over real asset names.
- Clone safety: bad URLs, path escapes, existing folders.
- Web: FastAPI test client, including a check that README HTML is sanitized and that state-changing requests without the session token are rejected.
- TUI: Textual's built-in test driver.
- One opt-in live smoke test against the real APIs, excluded from the default run.

## Out of scope for v1

Accounts or login flows, starring or forking from inside the app, other hosts (Codeberg, Bitbucket), recommendations, and packaging as an installable app.
