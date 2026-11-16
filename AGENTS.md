# AGENTS.md

This file provides guidance to AI coding agents (Claude Code, Antigravity, OpenCode, Codex, Copilot) when working with code in this repository. CLAUDE.md, GEMINI.md, and .cursorrules are symlinks pointing to this file.

## 1. Project Overview
RepoHub is a local-first, store-style repository browser and package aggregator for **GitHub, GitLab, Codeberg**, and custom Forgejo/Gitea instances. It unifies code exploration across multiple hosts into a single ranked search, curated shelves, rich repo pages with ARM64 release detection, and sandboxed safe cloning.

### Frontends & Entry Points
- `repohub` (`src/repohub/cli.py`): Scriptable command-line interface with table and JSON outputs.
- `repohub-web` (`src/repohub/web/app.py`): Local web application powered by FastAPI, Jinja2, and htmx on `http://127.0.0.1:8765`.
- `repohub-tui` (`src/repohub/tui/app.py`): Keyboard-centric terminal user interface built on Textual.
- `repohub-mcp` (`src/repohub/mcp.py`): Model Context Protocol (MCP) server providing repo search and inspection tools for AI coding assistants.

---

## 2. Architecture & Modules (`src/repohub/`)

- **Core & Aggregation (`src/repohub/core/`):**
  - `hub.py`: Main orchestrator querying GitHub, GitLab, and Forgejo providers concurrently and ranking results.
  - `providers/`: Normalized API clients (`github.py`, `gitlab.py`, `forgejo.py`) with error isolation (failure on one host does not block others).
  - `search.py`: Unified search syntax parser (`lang:rust stars:>500 nofork sort:updated`).
  - `catalog.py`: Curated discovery shelves and custom YAML shelf definitions.
  - `favorites.py`: Local SQLite/JSON persistence for user wishlists.
  - `clone.py`: Safe-clone engine verifying destination paths and restricting protocols to HTTPS on authorized hosts.

- **Web Frontend (`src/repohub/web/`):**
  - Fast-rendering server-driven UI with htmx and the Calm Slate design system (dark/light themes).

- **Terminal TUI (`src/repohub/tui/`):**
  - Responsive Textual layout with live search, repository inspection tabs, and modal dialogs.

---

## 3. Invariants & Security Directives

1. **Local-First & Zero Telemetry:** RepoHub runs strictly on the local machine and binds to `127.0.0.1`. Never introduce phone-home tracking or telemetry.
2. **Safe Clone Sandbox:** Clones are shallow (`--depth 1`) over HTTPS. Only hosts explicitly configured in `hosts.yaml` or standard providers (`github.com`, `gitlab.com`, `codeberg.org`) are permitted. Never execute post-clone scripts.
3. **No Live Network in Tests:** All unit and integration tests must run offline using `respx` mocks. Never make live network requests in the test suite.

---

## 4. Build, Test & Packaging Commands

```bash
# Run full automated test suite (1,670+ tests)
pytest tests/ -q

# Run web frontend
repohub-web --port 8765

# Launch terminal TUI
repohub-tui

# Run scriptable CLI
repohub search "package manager" --json

# Build distribution wheel
python -m build
```
