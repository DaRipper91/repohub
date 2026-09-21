# RepoHub roadmap and Phase 1 design

Date: 2026-09-21
Status: Approved design for Phase 1, not yet implemented. Later phases are agreed direction; each gets its own design before it is built.

## Where this comes from

RepoHub v1 (search, shelves, repo pages, favorites, safe clone; web and terminal apps) is built. This document records the next set of additions the owner chose, the order they will be built in, and the detailed design for Phase 1.

## Roadmap

| Phase | Scope | Notes |
|---|---|---|
| **1. Foundations** | Filters and sorting in both apps; curated shelves of exact repos plus the 171-project catalog as shelves; a scriptable CLI | This document |
| **2. Machine awareness** | "Already cloned" badges; a "Can I run this here?" panel (arm64 release assets, installed toolchains); a shared **project detector** (Cargo.toml, pyproject/requirements, package.json, Makefile, Dockerfile) | The detector is reused by Phase 3 |
| **3. Guided install and run** | Shows the exact commands for a cloned repo, the user approves each one, output streams live, commands run only inside the cloned folder | Decision: **guided, per-command approval on the host**. Not sandboxed, not suggest-only. Needs the strictest review, because it runs the repository's own code |
| **4. Favorites 2.0** | Tags, notes, collections, and a "new releases" tab | Reuses the existing release fetching and background refresh |
| **5. Claude Code** | An MCP server (read-only by default), "Open in Claude Code" after cloning, and a `/repohub` skill on top of the CLI | Cloning or installing through Claude Code must be separate tools that are approved individually |
| **6. UI redesign** | Improve the look of the web and terminal apps | Raised by the owner: do it after the features above |

Order rationale: Phase 1 is easy and unlocks the CLI that Phase 5's skill needs; Phase 2 builds the detector that Phase 3 depends on; Phase 3 comes after the detector exists and gets the strictest review; Phase 6 last so the design covers the finished feature set.

## Standing rules for every phase

- The existing clone protections stay as they are. Guided install (Phase 3) is a separate, explicit, opt-in action that never happens as part of a clone.
- Every phase follows the same process as v1: a design, a task-by-task plan, a fresh implementer per task, and an independent review after each task (with a security pass for anything touching untrusted data, URLs, paths or processes), then a final whole-project review.
- No test touches the real network, real git, or the real user data directory.
- Untrusted text (API data, README content, YAML from the user) is never interpreted as markup or executed.

# Phase 1 design

## 1. Filters and sorting

- `SearchFilters` gains `sort` (`stars` default, `updated`, `forks`) and `hide_forks`.
- `Repo` gains `fork: bool = False`, taken from GitHub's `fork` field and from GitLab's `forked_from_project`. The default keeps cached rows from before this change loadable.
- Providers:
  - GitHub: `sort=` (`stars`, `forks`, `updated`) and the `fork:false` qualifier.
  - GitLab: `order_by` `star_count` or `last_activity_at`. It cannot order by forks or hide forks server-side, so those two are applied client-side.
- After the two hosts' results are merged, the chosen sort is applied across the merged list, ties broken as they are today. Sorting each host separately and interleaving would give a confusing order.
- Web: the search bar gets a sort dropdown and a "hide forks" checkbox; the URL carries `sort` and `hide_forks`.
- Terminal app: filters come from query syntax typed into the search box, for example `tui lang:rust stars:500 days:90 host:github sort:updated nofork`. A shared parser (in `core`) extracts `key:value` tokens and leaves the rest as the search text. Unknown keys stay as plain search text; a bad value (for example `stars:abc`) produces a clear message instead of a crash. The web search box accepts the same syntax so both apps behave alike.
- The cache key already covers every filter field, so new options cannot return stale, mismatched results.
- Tests: parser edge cases, per-provider parameters, merged sort order, client-side fork hiding, and that old cached rows without `fork` still load.

## 2. Curated shelves and the catalog import

- Shelves come from two places: the packaged defaults, and a personal file at `~/.config/repohub/shelves.yaml` (found through platformdirs). Personal shelves load after the defaults. This also removes the current limitation that editing shelves needs an editable install.
- A shelf is either search-based (`topic`, `query`, `min_stars`, `days`, `language`, as today) or curated: a `repos:` list of `host:owner/name` entries, each optionally with a short `note` shown on its card.
- Curated shelf data is snapshot plus lazy refresh:
  - Each entry stores a snapshot (description, stars, language, license, last push date) with an "as of" date, so the shelf renders instantly with no API calls.
  - Opening a shelf refreshes the visible entries (12 at a time, at most 8 requests concurrently) and updates the numbers. If a refresh fails, the snapshot stays and shows its "as of" date.
  - A large shelf therefore never fires one request per entry.
- Catalog import: the 171-project catalog is converted once into packaged curated shelves, one per category (for example "Catalog: Terminal & TUI"). Each entry's note is the catalog's one-line "why it's fun" text, and the stats are dated 2026-09-20. Only the generated result is stored in the repository.
- Web: curated shelves appear on the home page like the others (first 6 entries), and the shelf page pages through the rest. Terminal app: they appear in the shelf list.
- Validation: every entry passes the same host and slug checks used everywhere else. A bad entry produces an error naming the shelf and the entry number, not a crash. Notes and snapshot text are treated as untrusted text (control characters stripped, never interpreted as markup), like any other data shown.
- Tests: loading both shelf kinds, entry validation, snapshot rendering, lazy refresh including failures, the concurrency cap, and the personal-file merge order.

## 3. Scriptable CLI

- One `repohub` command with subcommands: `search`, `repo`, `shelves`, `shelf`, `favorites`. `repohub-web` and `repohub-tui` stay. Phase 1 is read-only: no clone, install or favorite changes.
- `repohub search "terminal ui" --lang rust --min-stars 500 --days 90 --host github --sort updated --no-forks --limit 20`. The `key:value` query syntax from section 1 also works in the text.
- Two output modes:
  - Default: a readable plain-text table.
  - `--json`: one stable JSON document with a `schema_version` and one object per repo (the `Repo` fields). Search also includes `errors` (per-host failures) and `stale`. In JSON mode only JSON goes to stdout; diagnostics go to stderr, so output is safe to pipe.
- `repohub repo github:owner/name`: prints the detail. `--readme` adds the (capped) README. JSON output includes the latest release with each asset and its architecture, so a script can ask whether an arm64 build exists.
- Exit codes: `0` success, `3` partial (results printed but one host failed), `1` error or not found, `2` usage error.
- Safety: tokens are never printed; human output cannot be tricked by control characters (data is already stripped at the provider boundary and the table never interprets markup). The CLI shares the apps' SQLite cache.
- Purpose later: the Claude Code skill (Phase 5) drives this CLI, and the MCP server calls the same core underneath.
- Tests: flag-to-filter mapping, JSON schema stability, exit codes, partial failure, and a quiet stdout in JSON mode. A fake hub is injected, so nothing touches the network.

## Out of scope for Phase 1

Cloning, installing or changing favorites from the CLI; the project detector and "run here" panel (Phase 2); tags, notes and collections (Phase 4); the MCP server, "Open in Claude Code" and the skill (Phase 5); any visual redesign (Phase 6).

## Risks and open points

- GitHub search is rate limited without a token (about 10 requests a minute); the new sort and filter options add no extra requests, but the curated-shelf refresh does, which is why it is capped and lazy.
- GitLab cannot sort by forks or hide forks server-side, so those two options only apply to the results already fetched and can shrink a page of results.
- `Repo.fork` changes the model; old cached rows must keep loading (covered by a test).
