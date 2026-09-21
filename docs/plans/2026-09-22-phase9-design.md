# Phase 9: Claude Code integration

Three parts, as chosen earlier: a read-only MCP server, "Open in Claude Code", and a `/repohub` skill.

## MCP server (`repohub mcp`)

- Hand-written stdio JSON-RPC (newline-delimited), no new dependency. Methods: `initialize`, `ping`, `tools/list`, `tools/call`; notifications ignored. Protocol versions 2025-06-18, 2025-03-26, 2024-11-05.
- **Read-only.** Tools: search, repo, favorites, shelves, shelf, recommend, similar, cloned, check, plan, hosts. Nothing writes to a host, changes favorites/tags/notes/settings, clones, or runs a command. `plan` returns the proposal text only. Accounts are not exposed.
- Everything the server returns that came from a host is **third-party text**. Each result carries a notice saying so ("data, not instructions"), strings are sanitised, READMEs are off unless asked for (20 000 characters max), and results are size-capped (60 000 characters).
- Arguments are validated strictly (types, lengths, ranges, unknown keys refused); repository arguments must be `HOST:OWNER/NAME` on a configured host. Tokens never appear in any output. stdout carries protocol messages only; diagnostics go to stderr.
- History is never recorded by the server.

## Open in Claude Code

- Terminal app: `o` on a cloned repository asks `y`/`n`, then suspends RepoHub and starts `claude` in that folder (the user's own tool, with their normal environment). Where the terminal cannot suspend, it shows the command instead.
- Web app: cannot open a terminal, so a cloned repository's page shows the exact `cd '<folder>' && claude` command to copy. Nothing runs.

## Skill

`src/repohub/claude/skills/repohub/SKILL.md`, shipped with the package. `repohub claude-setup` prints the exact commands to register the MCP server and install the skill; it changes nothing itself.

## Tasks

1. `mcp.py` server, tools, validation, sanitising, size caps; `repohub mcp` and `repohub-mcp` entry points.
2. Skill file and `repohub claude-setup`.
3. "Open in Claude Code": TUI key, web command block.
4. Tests (protocol, every tool, refusals, sanitising, no writes), independent review, README, v0.11.0.
