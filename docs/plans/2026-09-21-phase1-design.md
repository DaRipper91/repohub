# RepoHub roadmap and Phase 1 design

Date: 2026-09-21
Status: Phase 1 is implemented (see the end of this document for the changes made along the way). Later phases are agreed direction; each gets its own design before it is built.

## Where this comes from

RepoHub v1 (search, shelves, repo pages, favorites, safe clone; web and terminal apps) is built. This document records the next set of additions the owner chose, the order they will be built in, and the detailed design for Phase 1.

## Roadmap

> **Update (2026-09-21):** the roadmap was re-ordered and renumbered. Other hosts, accounts, star and fork, and recommendations now come first (Phases 2 to 5), and the phases below shift to Phases 6 to 10. See `docs/plans/2026-09-21-hosts-accounts-design.md`. The table below is kept as the historical plan.

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

## Changes made during Phase 1 implementation

What differs from, or was added to, the design above, as found in the code:

- **Parser messages are sanitised and capped.** `queryparse.py` strips control and bidirectional characters from any user text echoed in a message and truncates it to 40 characters. At most 10 problems are kept, followed by one "... and more problems ignored" entry (11 in total). Messages are plain text and must never be rendered as markup.
- **ASCII-only numbers.** `stars:` and `days:` accept only ASCII digits (with an optional `>` or `>=`), so Unicode digits, `1_000` and `+5` are rejected. Values have a length and range limit.
- **Problem-message safety.** Problem text shown in the web app, terminal app and CLI is bounded and cleaned before display. The web app shows at most 11 search problems and a fixed number of shelf-file banners.
- **Snapshot-only home tiles.** `Hub.curated_page(..., refresh=False)` returns stored snapshots with no provider calls. The web home page uses it for curated shelf tiles because live refreshes would spend the API rate limit; opening a shelf refreshes the visible entries.
- **Personal shelves file hardening.** Only regular files are read (a FIFO or device is rejected before opening), with a 1 MB size limit, valid UTF-8 required, at most 200 shelves and 1000 entries per shelf, strict validation of keys, types, hosts and slugs, and bounded text lengths. Unquoted YAML dates are accepted for `as_of` and `pushed_at` and converted to text. Any failure skips the whole personal file and is reported as a problem instead of stopping the app.
- **Terminal notifications are plain text.** Every `notify(...)` call in the terminal app passes `markup=False`.
- **CLI output safety.** In `--json` mode, dangerous characters (C1 controls, bidirectional controls, line and paragraph separators) are written as `\uXXXX` escapes, and stdout carries only the JSON document. Table cells are sanitised and truncated (descriptions to 60 characters, slugs to 60, languages to 20), and error messages are capped at 300 characters.
- **`--no-refresh`.** `repohub shelf` gained `--no-refresh`, which shows a curated shelf from its stored snapshots with no API calls.

### Follow-up after the whole-phase review

- **Rate-limit backoff.** `Hub` remembers per host when a `RateLimited` error was seen (`clock` is injectable). For at least 60 and at most 3600 seconds (following the host's reset time when it is known), `curated_page` makes no calls to that host: its entries keep their snapshots and the stored error message is reported. Queued entries re-check this after taking a concurrency slot; other hosts are unaffected.
- **Personal shelf validation.** For search shelves, `language` and `topic` must match the same patterns as the query tokens (`LANG_RE`, `TOPIC_RE`, now public; `topic` is lowercased), `query` is cleaned, single-line and at most 200 characters (longer is an error), and a `repos` key that is empty or null is an error instead of becoming a search shelf.
- **Checkbox parsing.** The web `hide_forks` and `archived` values `""`, `0`, `false`, `off` and `no` (any case) mean off; any other value means on, and the form shows the same state.
- **Shared constants.** `HOSTS`, `MAX_STARS` and `MAX_DAYS` are defined once in `core/models.py` and imported by the parser, shelves, web app and CLI.
- **CLI.** All parser problems (up to 10 plus the "and more" marker) are printed as `Ignored:` lines; a digits-only shelf argument is an index when valid, otherwise a name (first match wins for duplicates); `search --limit` help notes the 30-per-host request cap.
- **Tests.** Added checks that shelf indexes agree across the web app, terminal app and CLI, that the real packaged catalog works offline through the CLI, that old cache rows without a `fork` key still load, and relaxed the wall-clock thresholds to 5 seconds.
