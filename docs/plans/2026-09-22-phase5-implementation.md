# Phase 5: recommendations - implementation plan

Design: Phase 5 in `2026-09-21-hosts-accounts-design.md`. Everything is computed locally; only ordinary topic/language searches leave the machine.

## Decisions made while planning

- **Signals:** favorites (weight 3), starred repositories (2), opt-in history (1). Starred repos are read from each signed-in host (at most 200 per host, two pages), kept **in memory** for an hour, never written to disk.
- **History:** off by default. A setting plus a `history` table in the existing database, capped at 500 entries and 90 days, with clear. Recorded only when a repository page is opened in the web or terminal app while it is on.
- **Profile:** weights per topic, language and host. Computed on demand, never stored.
- **Candidates:** at most 3 searches (the profile's top topics, each with the top language when it has one), through the existing `Hub.search` (so caching, deadlines and rate-limit handling apply). Hosts in their rate-limit pause are skipped.
- **Ranking:** profile match (topic weights, language weight) plus a log-scaled popularity term; drop favorites, starred, seen, archived and forks; at most 3 results per leading topic. Each result carries a plain-text reason.
- **Similar:** the repository's top 3 topics and its language, at most 3 searches, ranked by topic overlap, excluding itself and forks.
- **Where:** "Recommended for you" on the web home (loaded lazily, empty without a signal), "Similar repositories" on repo pages, a "Recommended" row in the terminal shelf list, and read-only `repohub recommend` / `repohub similar HOST:OWNER/NAME`. History toggle and clear live on the Accounts page (web) and F3/F4 (terminal); the CLI stays read-only.

## Tasks

1. `History` store with the opt-in setting (cap, expiry, clear).
2. `starred_repos()` on the three providers.
3. `core/recommend.py`: profile, scoring, diversity cap, explanations, similar ranking (pure functions).
4. Hub: `recommend()`, `similar()`, `record_view()`, in-memory starred cache.
5. Web: home fragment, similar fragment, history controls (POST + session token).
6. Terminal app: Recommended row, history keys.
7. CLI: `recommend`, `similar`.
8. Tests, independent spec + security review, README/version 0.6.0, commit, ask before pushing.
