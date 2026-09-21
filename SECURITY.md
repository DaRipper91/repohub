# Security policy

RepoHub renders untrusted data (repository descriptions, READMEs, release names) and can run `git clone`, so security reports are welcome.

## Reporting a vulnerability

Please report privately through GitHub: **Security → Report a vulnerability** on this repository (available once the repository is public and private reporting is enabled). Until then, contact the maintainer, [@DaRipper91](https://github.com/DaRipper91), directly. Do not put vulnerability details in a public issue; if you need a private channel, open an issue that says only that.

Helpful details: what you did, what you expected, what happened, the RepoHub version (`git rev-parse HEAD`), and whether it needs a hostile repository, a crafted URL, or a browser page to trigger.

## What is in scope

- Script or markup injection through repository data in the web app or the terminal app.
- Bypassing the session-token, Host-header or Content-Security-Policy protections of the web app.
- Anything that makes `clone` run outside the chosen folder, contact a host that is not configured (the built-in `github.com`, `gitlab.com` and `codeberg.org`, plus hosts from `hosts.yaml`), run install or build steps, or delete files it did not create.
- The search-query parser (`lang:`, `stars:` and the other tokens) in the web app, the terminal app and the CLI: injection through echoed problem messages, and resource use on hostile input.
- The personal shelves file (`shelves.yaml`) and the rendering of curated notes and snapshots: parsing, size limits, unsafe file types, and markup or control characters in text.
- The CLI's JSON and text output: control and bidirectional characters, JSON escaping, and anything that could print secrets.
- The personal hosts file (`hosts.yaml`): parsing, size limits, unsafe file types, duplicate keys, and URL validation (https only; no credentials, port, path, IP literals or `localhost`). Also the token-variable rule (extra hosts may only use `REPOHUB_*_TOKEN` variables, so a config file cannot bind an unrelated secret to a host) and the unique-host and unique-token checks.
- The Forgejo/Gitea provider: token isolation between hosts (a token is sent only to its own host), redirects not being followed, the clone allow-list, and hostile or malformed responses from a configured server (slugs, numbers, URLs and text are validated and cleaned at the boundary).
- Leaking API tokens (logs, error pages, cache, outgoing requests to the wrong host).

## Design notes

The protections that exist today are listed in the README under **Security**. RepoHub is a local, single-user tool: it binds to `127.0.0.1` by default and is not designed to be exposed to a network.
