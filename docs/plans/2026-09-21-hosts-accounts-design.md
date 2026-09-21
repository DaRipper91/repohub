# Design: other hosts, accounts, star and fork, recommendations (Phases 2 to 5)

Date: 2026-09-21
Status: Approved design. Phase 2 is next to be built; Phases 3 to 5 follow in order, each with its own plan and reviews.

## Where this comes from

After Phase 1 (v0.2.0) the owner asked to work next on the README's "Not included yet" features: other hosts, accounts and login, starring and forking from inside the app, and recommendations. The earlier "Phase 2: machine awareness" is paused and moves later in the roadmap.

## Decisions

| Question | Decision |
|---|---|
| Which extra hosts | The **Forgejo/Gitea family**: Codeberg built in, plus any Forgejo or Gitea instance added in a config file. Bitbucket is left out for now |
| How sign-in works | **Use existing sign-ins, store nothing**: env variables and the `gh` CLI. RepoHub never writes a secret to disk. No OAuth apps |
| Where star and fork exist | **Web and terminal apps only**. The CLI stays strictly read-only |
| Recommendation signals | All four: favorites, starred repos, an opt-in local history, and "similar to this repo" |
| Order | Hosts, then accounts, then star and fork, then recommendations. Pause the machine-awareness phase |

## Roadmap after this change

| Phase | What | Status |
|---|---|---|
| v1, 1 | Search, shelves, favorites, safe clone, web + terminal apps; filters and sorting, curated shelves, CLI | Done (v0.2.0) |
| **2. Other hosts** | Host registry; Forgejo/Gitea provider; Codeberg built in; extra instances in a config file | Next |
| **3. Accounts and login** | Existing sign-ins per host; an Accounts page (who, token source, scopes, rate limit) | Planned |
| **4. Star and fork** | Star, unstar and fork from the web and terminal apps, always confirmed, with a local action log | Planned |
| **5. Recommendations** | Favorites, stars, opt-in history and "similar repos", all computed locally | Planned |
| 6. Machine awareness | "Already cloned" badges (scan the clone folder only), "Can I run this here?" panel, project detector | Paused |
| 7. Guided install and run | Approve each command; runs the repository's own code | Planned |
| 8. Favorites 2.0 | Tags, notes, collections, new-releases tab | Planned |
| 9. Claude Code | Read-only MCP server, "Open in Claude Code", `/repohub` skill | Planned |
| 10. UI redesign | Look of the web and terminal apps | Planned, last |

Phase 6 decision already made: the clone scan looks only at the clone folder (`REPOHUB_CLONE_DIR`, default `~/playground`) and reads each subfolder's `.git/config` for its origin URL.

## Standing rules (unchanged, apply to every phase)

- The clone protections stay as they are (https only, strict URL and path checks, locked-down git environment, cleanup only of what a call created). Extending them to more hosts changes only the allow-list, never the checks.
- The process for every phase: a design, a task-by-task plan, a fresh implementer per task, an independent review after each task (with a security pass for anything touching untrusted data, URLs, files, processes or accounts), then a final whole-phase review.
- No test touches the real network, real git, real tokens, or the real user data and config directories.
- Untrusted text (API data, README content, YAML from the user) is never interpreted as markup or executed.
- Write actions on a user's accounts (Phase 4) never run from a GET, are always confirmed, and are never retried automatically.

## Facts verified against a real Forgejo server (codeberg.org, 2026-09-21)

- `GET /api/v1/repos/search?q=..&sort=stars&order=desc&limit=N&mode=source&archived=false` returns `{"ok": true, "data": [...]}`. Each repo has `full_name`, `html_url`, `description`, `stars_count`, `forks_count`, `language`, `topics`, `fork`, `archived`, `updated_at`, `website`, `default_branch`, `has_releases`. **There is no license field**, and `GET /repos/{o}/{r}/licenses` returns 404 on this server version, so license shows as unknown.
- Pagination uses a `Link` header and `X-Total-Count`. No rate-limit headers were seen on anonymous calls.
- `GET /repos/{o}/{r}/raw/README.md` returns the README (file names are case-sensitive: `readme.md` gave 404); `?ref=` is accepted.
- `GET /repos/{o}/{r}/releases/latest` returns `tag_name`, `published_at` and `assets` with `name`, `size`, `browser_download_url`.
- `GET /user`, `PUT /user/starred/{o}/{r}` and `POST /repos/{o}/{r}/forks` exist and return 401 without a token.
- Server version at the time: Forgejo 16 dev (Gitea 1.22 compatible).

The GitHub and GitLab write and identity endpoints are checked against the real APIs (read-only calls) when Phases 3 and 4 are planned; their write paths are tested with mocks.

# Phase 2: other hosts

## Host registry

- Today `("github", "gitlab")` is hard-coded in several modules. It becomes a registry of hosts, each with: `id`, `kind` (`github`, `gitlab`, `forgejo`), display name, web domain, API base URL, and the token environment variable.
- Built in: `github`, `gitlab`, and `codeberg` (kind `forgejo`, `https://codeberg.org`).
- Extra instances go in `hosts.yaml` in the user config directory (found with platformdirs, next to `shelves.yaml`):
  `- {id: myforge, kind: forgejo, url: https://git.example.org, token_env: MYFORGE_TOKEN}`.
  Rules: `url` must be `https` with a plain hostname (no credentials, port allowed, no path other than an optional base path checked strictly), ids are `[a-z0-9-]{1,20}` and cannot clash with built-in ids, at most 20 extra hosts, file size limit, regular files only. A broken file never stops the app: the problem is shown and the file is skipped (same handling as the personal shelves file).
- Repo keys keep the shape `host:owner/name`, with the host id.
- Every place that lists hosts (query syntax `host:`, the web dropdown, the CLI `--host`, badges, the `Repo.host` validation, curated shelf entries, favorites, cache keys) reads the registry. A shelf entry or favorite for a host that is no longer configured stays stored and is shown as unavailable, not deleted.

## Forgejo/Gitea provider

- Search through `/repos/search`: `q`, `sort` (`stars`, `updated`), `order=desc`, `limit`, `mode=source` to hide forks, `archived=false`, and the topic mode for `topic:`. Language, minimum stars, recency and sorting by forks are applied client-side because the endpoint does not filter on them.
- Detail: the repo JSON, the README by trying the usual file names against `/raw/`, and `/releases/latest` (404 means none). License is unknown.
- Same safety as the existing providers: redirects are not followed, tokens are sent only to their own host, slugs are validated, malformed responses become `ProviderError`, text passes through `clean_text` and URLs through `safe_url`, and rate-limit responses use the existing pause logic.
- `Authorization: token <t>` is sent only when a token exists for that host.

## Everywhere else

- Search queries all enabled hosts concurrently; the merge, sort and dedupe logic stays the same.
- Cloning is allowed only from configured hosts' domains, with the existing strict URL rules.
- Web, terminal app and CLI show a badge per host; tests use fixtures modeled on the real Codeberg responses above.

# Phase 3: accounts and login

- No secrets are stored. Tokens are found per host from env variables and the `gh` CLI: `GITHUB_TOKEN`, `GH_TOKEN`, `gh auth token`, `GITLAB_TOKEN`, `CODEBERG_TOKEN`, and each extra host's `token_env`.
- An **Accounts** page in the web app (`/accounts`), a key in the terminal app, and a read-only `repohub accounts` command. For each host it shows who you are signed in as (`GET /user`), where the token came from (for example "env GITLAB_TOKEN" or "gh CLI", never the token), the token's scopes where the host reports them, and the rate limit where the host exposes it.
- A "can I star and fork here?" hint is derived from what the host reports, with the exact fix command when one exists, and "unknown" when a host does not expose scopes.
- Identity and scope lookups are cached for a few minutes and never stored on disk. The page only reads.
- Tests: identity, scope and error cases per host with mocked responses, and checks that no token appears in any output.

# Phase 4: star and fork

- Web and terminal apps only. The CLI stays read-only, and so does anything built on it later.
- GitHub and Forgejo: `PUT` and `DELETE /user/starred/{owner}/{repo}`, and `POST /repos/{owner}/{repo}/forks`. GitLab: its star, unstar and fork endpoints. The exact GitLab and GitHub calls are verified read-only against the real APIs when this phase is planned.
- Every action shows a confirmation naming the host, the repository and the account; fork adds a warning that it creates a repository in the account that RepoHub will never delete. No bulk actions, and nothing happens as a side effect of another action.
- Web writes are POSTs carrying the per-session token; the terminal app uses `y` and `n`. Writes are never retried automatically.
- The repo page shows the star state when signed in (star, starred, unstar) and a Fork action; after a fork it links to the new repository. Cached stats for that repository refresh after an action.
- Clear failures: a missing permission points to the Accounts page, rate limits use the existing pause, and "already starred" counts as success.
- A local action log (host, repository, action, time, result; no secrets) is shown as "Recent actions" on the Accounts page.
- Tests: per host with mocks; confirmation required; a missing session token gives 403; idempotency; permission errors; no retries; log entries.

# Phase 5: recommendations

- Signals, all local: favorites; starred repos read from each signed-in host (capped at about 200, cached an hour); an opt-in history of repositories opened (off by default, capped at 500 entries and 90 days, with a Clear button and a toggle on the Accounts page); and "similar to this repo".
- Method: build an interest profile from the topics, languages and hosts in those signals (favorites weigh most, then stars, then recent history). Run at most 3 searches with the existing machinery, merge, drop anything already saved, starred or seen, and rank by profile match plus popularity with at most 3 repositories per topic. No machine learning and no outside service. Each result says why it was recommended.
- "More like this": a repository's top topics and language, excluding itself and forks, ranked by topic overlap.
- Where: a "Recommended for you" shelf on the web home (only when there is a signal), a "Similar repositories" section on repo pages, a "Recommended" entry in the terminal shelf list, and read-only `repohub recommend` and `repohub similar HOST:OWNER/NAME` commands.
- Rate-limit aware: cached for an hour, and skipped while a host is in its rate-limit pause.
- Privacy: the profile is computed on demand and never stored; only the opt-in history is stored. The only data that leaves the machine is the ordinary API searches, which contain topic and language keywords.
- Tests: deterministic scoring, exclusions, the diversity cap, sanitised explanations, history opt-in, clear and cap, and the rate-limit skips.

## Risks and open points

- Forgejo servers differ by version (Codeberg was checked). Fields such as licenses may exist elsewhere; the provider must tolerate missing fields.
- Self-hosted instances are user-configured input: URL validation and the no-redirect rule matter more than for the built-in hosts.
- Scopes and rate limits are not exposed by every host; the Accounts page must say "unknown" rather than guess.
- Recommendation quality depends on the data available; the explanations exist so a poor result is easy to understand.
