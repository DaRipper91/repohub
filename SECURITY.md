# Security policy

RepoHub renders untrusted data (repository descriptions, READMEs, release names) and can run `git clone`, so security reports are welcome.

## Reporting a vulnerability

Please report privately through GitHub: **Security → Report a vulnerability** on this repository. Do not open a public issue for a vulnerability.

Helpful details: what you did, what you expected, what happened, the RepoHub version (`git rev-parse HEAD`), and whether it needs a hostile repository, a crafted URL, or a browser page to trigger.

## What is in scope

- Script or markup injection through repository data in the web app or the terminal app.
- Bypassing the session-token, Host-header or Content-Security-Policy protections of the web app.
- Anything that makes `clone` run outside the chosen folder, contact a host other than `github.com` or `gitlab.com`, run install or build steps, or delete files it did not create.
- Leaking API tokens (logs, error pages, cache, outgoing requests to the wrong host).

## Design notes

The protections that exist today are listed in the README under **Security**. RepoHub is a local, single-user tool: it binds to `127.0.0.1` by default and is not designed to be exposed to a network.
