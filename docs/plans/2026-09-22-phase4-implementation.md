# Phase 4: star and fork - implementation plan

Design: `2026-09-21-hosts-accounts-design.md` (Phase 4). Web and terminal apps only; the CLI stays read-only.

## Endpoints (checked against the vendor docs on 2026-09-22)

| Host | Star state | Star | Unstar | Fork |
|---|---|---|---|---|
| GitHub | `GET /user/starred/{o}/{r}` 204 starred, 404 not | `PUT` same path, 204 (needs `Content-Length: 0`) | `DELETE` same path, 204 | `POST /repos/{o}/{r}/forks`, 202, returns the fork |
| GitLab | no direct endpoint: `GET /projects/:id/starrers?search=<login>`, exact login match | `POST /projects/:id/star`, 200 (304 = already) | `POST /projects/:id/unstar`, 200 (304 = not starred) | `POST /projects/:id/fork`, asynchronous; 409 = name taken or fork exists |
| Forgejo/Codeberg | same paths as GitHub (Gitea-compatible). **Not verified against the docs** (fetch failed): confirm on first real use | same | same | `POST /repos/{o}/{r}/forks`, 202 |

The vendor docs do not state token scopes for these calls, so the Accounts hint from Phase 3 stays a hint, and a 403 is reported as "missing permission, see Accounts".

## Rules

- Writes go through one provider helper: exactly one attempt, redirects not followed, the token is never dropped or replaced by anonymous access, no retry on any error.
- Nothing runs from a GET. Web writes are POSTs carrying the session token; the terminal app asks `y`/`n`.
- Every action needs a signed-in account (Phase 3 `Hub.account`) and is refused while the host is in its rate-limit pause.
- "Already starred" and "not starred" count as success. A fork that already exists is reported as a plain error (except GitHub, which returns the existing fork).
- The local action log holds host, repository, action, time and result only. No token, no response bodies. Capped at 200 rows.
- After an action, cached repository and detail entries for that repository are dropped so stats refresh.

## Tasks

1. Providers: `starred`, `star`, `unstar`, `fork` for GitHub, GitLab, Forgejo, with a shared write helper and typed failures (permission, not found, rate limit, conflict).
2. `ActionLog` store (sqlite, same database file) and `Cache.delete`.
3. Hub: `starred`, `set_star`, `fork` with sign-in check, rate-limit pause, logging, cache invalidation.
4. Web: confirmation pages (GET, no effect), POST endpoints, star state and Fork on the repo page, "Recent actions" on the Accounts page.
5. Terminal app: `s` star/unstar and `k` fork on the detail screen, each behind a `y`/`n` confirmation; recent actions in the accounts view.
6. Tests per task (mocked HTTP only; confirmation required; missing session token gives 403; idempotency; permission errors; no retries; log entries).
7. Independent spec and security review, fixes, README/version (0.5.0), commit and push.
