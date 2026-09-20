# RepoHub

RepoHub is a store-style hub for browsing GitHub and GitLab repositories. Search both hosts at once, browse curated shelves, read READMEs and release info, keep favorites, and clone a repository safely. It comes as a local web app (FastAPI, Jinja, htmx) and a terminal app (Textual). Both use the same core library.

## Install

Requires Python 3.11 or newer (developed on 3.12).

```
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -e ".[dev]"
```

## Run

```
.venv/bin/repohub-web     # http://127.0.0.1:8765 (options: --host, --port)
.venv/bin/repohub-tui
```

`repohub-web --help` lists its options. `repohub-tui` has no options; it starts the terminal app immediately.

## Tokens

Tokens are optional but raise API rate limits.

- GitHub: `GITHUB_TOKEN`, then `GH_TOKEN`, then the output of `gh auth token` if the GitHub CLI is installed.
- GitLab: `GITLAB_TOKEN`.

Tokens are held in memory only, sent only to the matching API host, and never logged. Surrounding whitespace is stripped. Without a token you are anonymous and rate limits are tight (GitHub search allows only about 10 requests a minute); the UI shows a per-host banner and serves cached results.

## Configuration

- `REPOHUB_CLONE_DIR`: where clones go. Default `~/playground`.
- Shelves: edit `src/repohub/core/shelves.yaml`. Each shelf has a `name`, optional `topic`, `min_stars` and `days` (recent push window).
- Cache and favorites live in a SQLite file `repohub.db` in your user data directory (via platformdirs).

## Security

- The web server binds to loopback (`127.0.0.1`) by default and only accepts the Host headers `127.0.0.1` and `localhost`. Passing a non-loopback `--host` prints a warning.
- Favorite and clone POSTs require a per-session token.
- READMEs are rendered with markdown-it and sanitised with nh3; link URLs are validated. A Content-Security-Policy restricts scripts and other resources to the app itself.
- The terminal app renders all untrusted text as plain `Text` (no markup interpretation) and opens links only when they are http or https.
- Control characters are stripped from provider data at the provider boundary.
- README text is capped at 200,000 characters.
- Cloning is restricted: only `https` URLs on `github.com` or `gitlab.com`; shallow (`--depth 1`); no install or build steps are run; the URL is validated strictly and rebuilt in canonical form; git runs with a locked-down environment (no prompts, https-only protocol, no system or global git config); a failed clone is cleaned up.

## Tests

```
.venv/bin/pytest          # offline suite; live tests are deselected
.venv/bin/pytest -m live  # opt-in smoke tests against the real GitHub and GitLab APIs
```

The live tests use a token found automatically as described above, or run anonymously and may hit rate limits.

## What v1 does not do

Out of scope (see `docs/plans/2026-09-20-repohub-design.md`): accounts or login flows, starring or forking from inside the app, other hosts (Codeberg, Bitbucket), recommendations, and packaging as an installable app.

Known limitations:

- Results from both hosts are deduplicated by `owner/name`, so different repositories with the same owner and name on the two hosts are merged.
- GitLab search results carry no language unless a language filter is used.
- Repositories with an unknown push date are excluded by recency filters.
- Remote images in READMEs can load in the web UI (the CSP allows `img-src *`), which exposes your IP address to those hosts.
- GitLab cannot express some filters, such as minimum stars, so they are applied client-side and a page of results can shrink after filtering.
