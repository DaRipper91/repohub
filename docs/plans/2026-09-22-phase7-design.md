# Phase 7: guided install and run

Status: design chosen by the maintainer's earlier decision (guided, approve each command, on the host, a separate opt-in that is never part of clone) plus the safeguards below, which were decided while designing.

## What it is

For a repository that is already cloned (Phase 6 finds it), RepoHub proposes the usual build steps for its project type. Nothing runs until the user approves **that one command**, after seeing the exact command, the folder, and a warning that it runs the repository's own code with the user's permissions.

## Safeguards

| Risk | Rule |
|---|---|
| Untrusted text becoming commands | Commands come only from a **fixed table** keyed by project type. Never from a README, a script name, or any API text. The only file content read is `package.json`, to see whether a `build` script exists (size-capped). |
| Running without consent | Off by default; a separate switch ("guided install") turns it on. Each command needs its own approval. The plan shown is re-derived at approval time and must match by digest, so files changed in between cannot swap the command. |
| Shell injection | argv lists only, never a shell. The executable is resolved on `PATH` to an absolute path first. |
| Leaking secrets | The child gets a minimal environment (PATH, HOME, LANG, TERM, TMPDIR, USER). No tokens, no `REPOHUB_*`, no `GITHUB_TOKEN`. |
| Escaping the folder | The working directory is the clone folder found by the Phase 6 scan, re-checked (real directory, not a symlink, origin still on a configured host) right before running. |
| Runaway processes | One run at a time; own process group; stdin closed; a time limit (30 minutes); output capped (200 KB, tail kept); Cancel kills the whole group. |
| Output tricks | Output is shown as plain text with ANSI escapes and control/bidi characters removed. |
| Web CSRF | POST + session token for every action; nothing runs from a GET. |
| CLI | Stays read-only: `repohub plan` only prints the proposal. |

Not promised: RepoHub cannot sandbox a build. A build script can do anything the user can. The warning says so.

## Command table (fixed)

- rust: `cargo build --release`
- python: `python3 -m venv .venv`, then `.venv/bin/pip install .` (pyproject/setup.py) or `.venv/bin/pip install -r requirements.txt`
- node: `npm install`, and `npm run build` only if package.json has a build script
- go: `go build ./...`
- cmake: `cmake -S . -B build`, `cmake --build build`
- meson: `meson setup build`, `meson compile -C build`
- make: `make` (only when no other build system is detected)
- docker, java, ruby: not proposed (they need a daemon, wrapper scripts, or system installs).

## Tasks

1. `marker_files`, a settings store, the fixed plan (`runplan.py`) with digest.
2. The runner (`runner.py`): validation, process group, env, timeout, output cap, cancel, action log ("run").
3. Web: opt-in switch, plan page, approve and run, live output page, cancel.
4. Terminal app: `i` on a repository, y/n per command, live output, cancel.
5. CLI `repohub plan HOST:OWNER/NAME` (read-only).
6. Tests (real short subprocesses via `sys.executable`, never real build tools or network), independent review, README, v0.9.0, commit; ask before pushing.
