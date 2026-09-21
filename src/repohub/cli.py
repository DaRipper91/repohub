"""The scriptable ``repohub`` command: read-only search, repo detail, shelves and favorites.

Argument parsing happens before anything heavy is imported or built, so ``--help`` and usage
errors never construct a Hub or read tokens. In ``--json`` mode stdout carries exactly one JSON
document; every diagnostic goes to stderr. All human-readable cells pass through ``clean_text``.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
from typing import Callable, TextIO

from repohub.core.auth import find_host_tokens
from repohub.core.browse import Shelf, load_all_shelves
from repohub.core.hosts import registry
from repohub.core.hostsconfig import configure_hosts
from repohub.core.models import MAX_DAYS, MAX_STARS, SORTS, Repo, SearchFilters
from repohub.core.providers.base import NotFound, ProviderError
from repohub.core.queryparse import parse_query
from repohub.core.textsafe import clean_text

SCHEMA_VERSION = 1
MAX_PROBLEMS_SHOWN = 11  # the parser keeps at most 10 problems plus a final "and more" marker
DESC_WIDTH = 60
MSG_CAP = 300

EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_PARTIAL = 0, 1, 2, 3


def _bounded(lo: int, hi: int) -> Callable[[str], int]:
    def conv(value: str) -> int:
        try:
            n = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError(f"invalid whole number: {clean_text(value)[:40]!r}") from None
        if not lo <= n <= hi:
            raise argparse.ArgumentTypeError(f"must be between {lo} and {hi}")
        return n
    return conv


def _version() -> str:
    # Same source as repohub-tui: the installed metadata can be stale in an editable install.
    from repohub import __version__

    return f"repohub {__version__}"


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="repohub", description="Search repositories on GitHub, GitLab, Codeberg and configured hosts (read-only).")
    p.add_argument("--version", action="version", version=_version())
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    s = sub.add_parser("search", help="search repositories",
                       description="Search repositories. The text also accepts key:value filters "
                                   "such as lang:rust stars:>500 days:90 host:github sort:updated nofork archived.")
    s.add_argument("text", nargs="+", metavar="TEXT")
    s.add_argument("--lang", metavar="L")
    s.add_argument("--min-stars", type=_bounded(0, MAX_STARS), default=0, metavar="N")
    s.add_argument("--days", type=_bounded(0, MAX_DAYS), default=None, metavar="N",
                   help="only repositories pushed within N days")
    # Not argparse `choices`: the valid ids depend on hosts.yaml, which is read only after parsing
    # (so --help, --version and usage errors never touch the file). Checked in _check_host.
    s.add_argument("--host", default=None, metavar="HOST",
                   help="both, all, or a configured host id (see 'repohub hosts')")
    s.add_argument("--sort", choices=SORTS, default=None)
    s.add_argument("--no-forks", action="store_true")
    s.add_argument("--archived", action="store_true", help="include archived repositories")
    s.add_argument("--limit", type=_bounded(1, 200), default=20, metavar="N",
                   help="show at most N results (each host returns at most 30 results per request)")
    s.add_argument("--json", action="store_true")

    r = sub.add_parser("repo", help="show one repository", description="Show one repository: HOST:OWNER/NAME.")
    r.add_argument("repo", metavar="HOST:OWNER/NAME")
    r.add_argument("--readme", action="store_true", help="include the README")
    r.add_argument("--json", action="store_true")

    sh = sub.add_parser("shelves", help="list shelves")
    sh.add_argument("--json", action="store_true")

    sf = sub.add_parser("shelf", help="show one shelf", description="Show a shelf by exact name or index.")
    sf.add_argument("shelf", metavar="NAME_OR_INDEX")
    sf.add_argument("--limit", type=_bounded(1, 50), default=20, metavar="N")
    sf.add_argument("--no-refresh", action="store_true",
                    help="curated shelves: use stored snapshots only, make no API calls")
    sf.add_argument("--json", action="store_true")

    fv = sub.add_parser("favorites", help="list stored favorites (no network)")
    fv.add_argument("--tag", metavar="TAG", help="only favorites with this tag")
    fv.add_argument("--collection", metavar="NAME", help="only favorites in this collection")
    fv.add_argument("--query", metavar="TEXT", help="only favorites whose name, description or note contains TEXT")
    fv.add_argument("--json", action="store_true")

    rl = sub.add_parser("releases", help="favorites with a release you have not seen yet (asks each host; read-only)",
                        description="Looks up the latest release of each favorite and lists those newer than the last "
                                    "one you marked as seen in the web or terminal app. The first check only records "
                                    "a starting point.")
    rl.add_argument("--json", action="store_true")

    ho = sub.add_parser("hosts", help="list configured hosts (no network)",
                        description="List configured hosts and whether a token is present. "
                                    "Token values are never shown.")
    ho.add_argument("--json", action="store_true")

    rc = sub.add_parser("recommend", help="repositories you may like (read-only)",
                        description="Suggestions from your favorites, your starred repositories and, if you turned it on, "
                                    "your local history. Computed on this machine; each result says why.")
    rc.add_argument("--limit", type=_bounded(1, 12), default=12, metavar="N")
    rc.add_argument("--json", action="store_true")

    sm = sub.add_parser("similar", help="repositories like one repository (read-only)",
                        description="Repositories that share topics with HOST:OWNER/NAME.")
    sm.add_argument("repo", metavar="HOST:OWNER/NAME")
    sm.add_argument("--limit", type=_bounded(1, 8), default=8, metavar="N")
    sm.add_argument("--json", action="store_true")

    cl = sub.add_parser("cloned", help="list repositories already cloned in your clone folder (no network)",
                        description="Looks only at the immediate subfolders of REPOHUB_CLONE_DIR (default ~/playground) "
                                    "and reads each one's .git/config for its origin URL.")
    cl.add_argument("--json", action="store_true")

    pl = sub.add_parser("plan", help="show the build commands RepoHub would propose for a cloned repository (never runs them)",
                        description="Prints the fixed, standard build steps for the project type found in the cloned "
                                    "folder. The CLI never runs them: use the web or terminal app, which ask you to "
                                    "approve each command.")
    pl.add_argument("repo", metavar="HOST:OWNER/NAME")
    pl.add_argument("--json", action="store_true")

    ro = sub.add_parser("roots", help="show the folders RepoHub looks in for clones; --scan suggests more (read-only)",
                        description="Lists the clone folder and any extra folders picked in the web or terminal app. "
                                    "--scan looks for folders that contain git clones (home folder, or --system for the whole "
                                    "filesystem), prints them and saves nothing. Pick folders in the apps.")
    ro.add_argument("--scan", action="store_true")
    ro.add_argument("--system", action="store_true", help="with --scan: the whole filesystem instead of the home folder")
    ro.add_argument("--json", action="store_true")

    ck = sub.add_parser("check", help="can I run this repository here? (read-only advice)",
                        description="A checklist and verdict for HOST:OWNER/NAME: release builds for this CPU, "
                                    "build tools on this machine, project type and whether it is already cloned. "
                                    "Nothing is run.")
    ck.add_argument("repo", metavar="HOST:OWNER/NAME")
    ck.add_argument("--json", action="store_true")

    ac = sub.add_parser("accounts", help="show who you are signed in as on each host (read-only)",
                        description="For each host: the account, where its token came from (never the token), "
                                    "its scopes and rate limit where the host reports them. "
                                    "Uses your existing sign-ins (env variables, gh CLI); stores nothing.")
    ac.add_argument("--json", action="store_true")
    return p


def _check_host(parser: argparse.ArgumentParser, value: str | None) -> None:
    if value is not None and value not in ("both", "all") and value not in registry().ids:
        parser.error("argument --host: not a configured host (see 'repohub hosts')")  # never echo input


# ---------------------------------------------------------------- formatting

def _cell(value: object) -> str:
    return clean_text(str(value) if value is not None else "")


def _msg(value: object) -> str:
    return _cell(value)[:MSG_CAP]


def _fit(text: str, width: int) -> str:
    text = _cell(text)
    return text if len(text) <= width else text[:width - 1] + "\u2026"


_JSON_ESCAPES = {c: f"\\u{c:04x}" for c in
                 [*range(0x7f, 0xa0), 0x2028, 0x2029, 0x061c, 0x200e, 0x200f,
                  *range(0x202a, 0x202f), *range(0x2066, 0x206a)]}


def _dumps(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2).translate(_JSON_ESCAPES)


def _short(text: str, width: int = DESC_WIDTH) -> str:
    text = _cell(text)
    return text if len(text) <= width else text[:width - 3] + "..."


def _date(value: str) -> str:
    return _cell(value)[:10]


def _table(headers: list[str], rows: list[list[str]], right: tuple[int, ...] = ()) -> str:
    rows = [[_cell(c) for c in row] for row in rows]
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]

    def fmt(row: list[str]) -> str:
        cells = [c.rjust(widths[i]) if i in right else c.ljust(widths[i]) for i, c in enumerate(row)]
        return "  ".join(cells).rstrip()

    return "\n".join([fmt(headers), *(fmt(r) for r in rows)])


def _repo_table(repos: list[Repo]) -> str:
    rows = [[_fit(r.slug, 60), _fit(r.host, 20), str(r.stars), _fit(r.language, 20), _date(r.pushed_at), _short(r.description)] for r in repos]
    return _table(["repo", "host", "stars", "language", "updated", "description"], rows, right=(2,))


def _size(n: int) -> str:
    return f"{n / 1_000_000:.1f} MB" if n >= 1_000_000 else f"{n / 1000:.0f} KB" if n >= 1000 else f"{n} B"


class _Out:
    def __init__(self, stdout: TextIO, stderr: TextIO):
        self.stdout, self.stderr = stdout, stderr

    def out(self, text: str = "") -> None:
        self.stdout.write(text + "\n")

    def err(self, text: str) -> None:
        self.stderr.write(_msg(text) + "\n")

    def json(self, obj: dict) -> None:
        self.stdout.write(_dumps(obj) + "\n")

    def problems(self, problems, prefix: str = "Ignored: ") -> None:
        for p in list(problems)[:MAX_PROBLEMS_SHOWN]:
            self.err(f"{prefix}{p}")

    def errors(self, errors: dict[str, str]) -> None:
        for host, message in errors.items():
            self.err(f"warning: {_cell(host)}: {_msg(message)}")


def _status(repos_count: int, errors: dict) -> int:
    if not errors:
        return EXIT_OK
    return EXIT_PARTIAL if repos_count else EXIT_ERROR


def _emit_list(o: _Out, as_json: bool, repos: list[Repo], errors: dict[str, str], stale: bool,
               extra: dict | None = None, repo_dicts: list[dict] | None = None) -> int:
    o.errors(errors)
    if stale:
        o.err("note: showing stale cached results")
    if as_json:
        doc = {"schema_version": SCHEMA_VERSION, **(extra or {}),
               "repos": repo_dicts if repo_dicts is not None else [r.to_dict() for r in repos],
               "errors": errors, "stale": stale}
        o.json(doc)
    elif repos:
        o.out(_repo_table(repos))
    else:
        o.err("no repositories found")
    return _status(len(repos), errors)


# ---------------------------------------------------------------- commands

def _cmd_search(args, hub, o: _Out) -> int:
    base = SearchFilters(language=args.lang, min_stars=args.min_stars,
                         updated_within_days=args.days or None,
                         hosts=SearchFilters().hosts if args.host in (None, "both", "all") else (args.host,),
                         include_archived=args.archived, sort=args.sort or "stars",
                         hide_forks=args.no_forks)
    parsed = parse_query(" ".join(args.text), base)
    o.problems(parsed.problems)
    result = asyncio.run(hub.search(parsed.text, parsed.filters))
    return _emit_list(o, args.json, result.repos[:args.limit], dict(result.errors), result.stale)


def _valid_repo_arg(value: str) -> tuple[str, str] | None:
    host, sep, slug = value.partition(":")
    if not sep or len(slug) > 200 or not registry().slug_ok(host, slug):
        return None
    return host, slug


def _cmd_repo(args, hub, o: _Out) -> int:
    parsed = _valid_repo_arg(args.repo)
    if parsed is None:
        o.err("error: repository must look like HOST:OWNER/NAME with a configured host (see 'repohub hosts')")
        return EXIT_USAGE
    host, slug = parsed
    try:
        detail = asyncio.run(hub.detail(host, slug))
    except NotFound:
        o.err(f"error: {host}:{_cell(slug)} not found")
        return EXIT_ERROR
    except ProviderError as e:
        o.err(f"error: {_cell(host)}: {_msg(e)}")
        return EXIT_ERROR
    readme = detail.readme if args.readme else None
    if args.json:
        o.json({"schema_version": SCHEMA_VERSION, "repo": detail.repo.to_dict(),
                "release": detail.release.to_dict() if detail.release else None, "readme": readme})
        return EXIT_OK
    r = detail.repo
    o.out(_cell(r.slug) + ("  [archived]" if r.archived else ""))
    o.out(f"host:      {_cell(r.host)}")
    o.out(f"url:       {_cell(r.url)}")
    o.out(f"stars:     {r.stars}   forks: {r.forks}")
    o.out(f"language:  {_cell(r.language) or '-'}   license: {_cell(r.license) or '-'}")
    o.out(f"pushed:    {_date(r.pushed_at) or '-'}")
    if r.homepage:
        o.out(f"homepage:  {_cell(r.homepage)}")
    if r.topics:
        o.out("topics:    " + ", ".join(_cell(t) for t in r.topics))
    o.out(f"about:     {_cell(r.description) or '-'}")
    rel = detail.release
    if rel:
        o.out(f"release:   {_cell(rel.tag)}" + (f" ({_date(rel.published_at or '')})" if rel.published_at else ""))
        for a in rel.assets:
            mark = " (arm64)" if a.arch == "arm64" else ""
            o.out(f"  - {_cell(a.name)}{mark}  {_size(a.size)}")
    else:
        o.out("release:   none")
    if readme is not None:
        o.out("")
        o.out(clean_text(readme, multiline=True))
    return EXIT_OK


def _kind(shelf: Shelf) -> str:
    return "curated" if shelf.curated else "search"


def _load(o: _Out) -> list[Shelf]:
    loaded = load_all_shelves()
    o.problems(loaded.problems, prefix="warning: ")
    return list(loaded.shelves)


def _cmd_shelves(args, hub, o: _Out) -> int:
    shelves = _load(o)
    entries = [{"index": i, "name": _cell(s.name), "kind": _kind(s), "entries": len(s.repos)}
               for i, s in enumerate(shelves)]
    if args.json:
        o.json({"schema_version": SCHEMA_VERSION, "shelves": entries})
    else:
        o.out(_table(["index", "name", "kind", "entries"],
                     [[str(e["index"]), e["name"], e["kind"], str(e["entries"])] for e in entries], right=(0, 3)))
    return EXIT_OK


def _find_shelf(shelves: list[Shelf], key: str) -> Shelf | None:
    low = key.strip().lower()
    # A digits-only argument is an index when it is a valid one; otherwise (or for any other text)
    # it is matched as a name, case-insensitively. With duplicate names the first shelf wins.
    if low.isascii() and low.isdigit() and int(low) < len(shelves):
        return shelves[int(low)]
    for s in shelves:
        if s.name.lower() == low:
            return s
    return None


def _cmd_shelf(args, hub, o: _Out) -> int:
    shelf = _find_shelf(_load(o), args.shelf)
    if shelf is None:
        o.err(f"error: no such shelf: {_cell(args.shelf)[:60]}")
        return EXIT_ERROR
    name = _cell(shelf.name)
    if not shelf.curated:
        result = asyncio.run(hub.shelf(shelf))
        return _emit_list(o, args.json, result.repos[:args.limit], dict(result.errors), result.stale,
                          extra={"shelf": name})
    page = asyncio.run(hub.curated_page(shelf, 0, args.limit, refresh=not args.no_refresh))
    if args.no_refresh:
        o.err("note: snapshot data, not refreshed")
    repos = [i.repo for i in page.items]
    dicts = [{**i.repo.to_dict(), "note": _cell(i.note), "as_of": i.as_of, "live": i.live} for i in page.items]
    errors = dict(page.errors)
    live = sum(1 for i in page.items if i.live)
    _emit_list(o, args.json, repos, errors, False, extra={"shelf": name}, repo_dicts=dicts)
    return EXIT_OK if not errors else (EXIT_PARTIAL if live else EXIT_ERROR)


def _cmd_favorites(args, hub, o: _Out) -> int:
    favs = hub.favorites
    tag = favs.clean_tag(args.tag) if args.tag else None
    if args.tag and not tag:
        o.err("error: not a valid tag")
        return EXIT_USAGE
    repos = favs.list(tag=tag, collection=args.collection, q=args.query)
    notes, tags, colls = favs.notes(), favs.tags_by_key(), favs.collections_by_key()
    dicts = [{**r.to_dict(), "note": _cell(notes.get(r.key, "")), "tags": [_cell(t) for t in tags.get(r.key, [])],
              "collections": [_cell(c) for c in colls.get(r.key, [])]} for r in repos]
    return _emit_list(o, args.json, repos, {}, False, repo_dicts=dicts)


def _cmd_releases(args, hub, o: _Out) -> int:
    errors = asyncio.run(hub.check_releases(force=True))
    items = hub.favorites.new_releases()
    o.errors(errors)
    if args.json:
        o.json({"schema_version": SCHEMA_VERSION, "new_releases": [
            {"host": _cell(r.host), "slug": _cell(r.slug), "release": _cell(t), "published": _cell(p)} for r, t, p in items],
            "errors": errors})
    elif items:
        o.out(_table(["repo", "host", "release", "published"],
                     [[_fit(r.slug, 50), _fit(r.host, 20), _fit(t, 30), _date(p)] for r, t, p in items]))
    else:
        o.err("no new releases")
    if not errors:
        return EXIT_OK
    return EXIT_PARTIAL if items else EXIT_ERROR


def _cmd_hosts(args, hub, o: _Out, problems: list[str] | None = None, gh_cli: Callable | None = None) -> int:
    reg = registry()
    kw = {"gh_cli": gh_cli} if gh_cli is not None else {}
    tokens = find_host_tokens(reg, **kw)
    # token_env lists variable NAMES (not secrets) so the user knows what to set; values never leave HostTokens.
    entries = [{"id": _cell(h.id), "kind": _cell(h.kind), "name": _cell(h.name), "domain": _cell(h.domain),
                "builtin": bool(h.builtin), "token": tokens.has(h.id),
                "token_env": [_cell(v) for v in h.token_env]} for h in reg.specs]
    shown = [_msg(p) for p in list(problems or [])[:MAX_PROBLEMS_SHOWN]]
    for p in shown:
        o.err(f"warning: {p}")
    if args.json:
        o.json({"schema_version": SCHEMA_VERSION, "hosts": entries, "problems": shown})
    else:
        o.out(_table(["id", "kind", "name", "domain", "builtin", "token"],
                     [[e["id"], e["kind"], e["name"], e["domain"], "yes" if e["builtin"] else "no",
                       "yes" if e["token"] else "no"] for e in entries]))
    return EXIT_OK


def _emit_recs(o: _Out, as_json: bool, result, extra: dict | None = None) -> int:
    o.errors(dict(result.errors))
    items = result.items
    if as_json:
        o.json({"schema_version": SCHEMA_VERSION, **(extra or {}), "recommendations": [i.to_dict() for i in items],
                "errors": dict(result.errors)})
    elif items:
        o.out(_table(["repo", "host", "stars", "language", "why"],
                     [[_fit(i.repo.slug, 50), _fit(i.repo.host, 20), str(i.repo.stars), _fit(i.repo.language, 20),
                       _short(i.why, 70)] for i in items], right=(2,)))
    else:
        o.err("no recommendations yet: favorite or star some repositories first" if not result.signal
              else "no recommendations found")
    return _status(len(items), dict(result.errors))


def _cmd_recommend(args, hub, o: _Out) -> int:
    return _emit_recs(o, args.json, asyncio.run(hub.recommend(args.limit)))


def _cmd_similar(args, hub, o: _Out) -> int:
    parsed = _valid_repo_arg(args.repo)
    if parsed is None:
        o.err("error: repository must look like HOST:OWNER/NAME with a configured host (see 'repohub hosts')")
        return EXIT_USAGE
    host, slug = parsed
    try:
        result = asyncio.run(hub.similar(host, slug, args.limit))
    except NotFound:
        o.err(f"error: {host}:{_cell(slug)} not found")
        return EXIT_ERROR
    except ProviderError as e:
        o.err(f"error: {_cell(host)}: {_msg(e)}")
        return EXIT_ERROR
    return _emit_recs(o, args.json, result, extra={"repo": f"{host}:{slug}"})


def _awareness():
    from repohub.config import clone_root
    from repohub.core.awareness import Awareness
    from repohub.core.roots import ScanRoots

    return Awareness(clone_root(), extra_roots=ScanRoots().list)


def _cmd_cloned(args, hub, o: _Out) -> int:
    root = _awareness().cloned(refresh=True)
    items = sorted(root.values(), key=lambda c: c.key)
    if args.json:
        o.json({"schema_version": SCHEMA_VERSION, "cloned": [{"host": _cell(c.host), "slug": _cell(c.slug),
                                                            "path": _cell(c.path)} for c in items]})
    elif items:
        o.out(_table(["repo", "host", "path"], [[_fit(c.slug, 50), _fit(c.host, 20), _fit(c.path, 80)] for c in items]))
    else:
        o.err("no cloned repositories found in the clone folder")
    return EXIT_OK


def _cmd_plan(args, hub, o: _Out) -> int:
    from repohub.core.runplan import WARNING, build_plan

    parsed = _valid_repo_arg(args.repo)
    if parsed is None:
        o.err("error: repository must look like HOST:OWNER/NAME with a configured host (see 'repohub hosts')")
        return EXIT_USAGE
    host, slug = parsed
    clone = _awareness().cloned(refresh=True).get(f"{host}:{slug.lower()}")
    if clone is None:
        o.err(f"error: {host}:{_cell(slug)} is not cloned in a known folder (see 'repohub cloned')")
        return EXIT_ERROR
    plan = build_plan(clone.path)
    if args.json:
        o.json({"schema_version": SCHEMA_VERSION, "repo": f"{host}:{slug}", "folder": _cell(clone.path),
                "steps": [{"id": st.id, "title": st.title, "command": st.text()} for st in plan.steps],
                "warning": WARNING})
        return EXIT_OK
    o.out(f"{_cell(slug)} in {_cell(clone.path)}")
    if plan.steps:
        for n, st in enumerate(plan.steps, 1):
            o.out(f"  {n}. {_cell(st.title)}: {_cell(st.text())}")
    else:
        o.out("  no standard build command is known for this project")
    o.err("note: proposal only. " + WARNING + " Run steps from the web or terminal app.")
    return EXIT_OK


def _cmd_roots(args, hub, o: _Out) -> int:
    from repohub.config import clone_root
    from repohub.core.roots import ScanRoots, discover, home_start

    if args.system and not args.scan:
        o.err("error: --system needs --scan")
        return EXIT_USAGE
    used = [str(clone_root()), *[r for r in ScanRoots().list() if r != str(clone_root())]]
    report = None
    if args.scan:
        from pathlib import Path

        report = discover(Path("/") if args.system else home_start())
    if args.json:
        doc = {"schema_version": SCHEMA_VERSION, "roots": [_cell(r) for r in used]}
        if report:
            doc["scan"] = {"start": _cell(report.start), "dirs_seen": report.dirs_seen, "truncated": report.truncated,
                           "seconds": report.seconds,
                           "found": [{"path": _cell(c.path), "repos": c.repos, "in_use": c.path in used}
                                     for c in report.candidates]}
        o.json(doc)
        return EXIT_OK
    o.out(_table(["folder", "status"], [[_fit(r, 90), "in use"] for r in used]))
    if report:
        o.err(f"scanned {report.dirs_seen} folders in {report.seconds}s below {_cell(report.start)}"
              + (" (stopped early)" if report.truncated else ""))
        if report.candidates:
            o.out("")
            o.out(_table(["found folder", "repos", "status"],
                         [[_fit(c.path, 90), str(c.repos), "in use" if c.path in used else "not used"]
                          for c in report.candidates], right=(1,)))
        else:
            o.err("no folders with clones found")
    return EXIT_OK


def _cmd_check(args, hub, o: _Out) -> int:
    parsed = _valid_repo_arg(args.repo)
    if parsed is None:
        o.err("error: repository must look like HOST:OWNER/NAME with a configured host (see 'repohub hosts')")
        return EXIT_USAGE
    host, slug = parsed
    try:
        detail = asyncio.run(hub.detail(host, slug))
    except NotFound:
        o.err(f"error: {host}:{_cell(slug)} not found")
        return EXIT_ERROR
    except ProviderError as e:
        o.err(f"error: {_cell(host)}: {_msg(e)}")
        return EXIT_ERROR
    v = _awareness().check(detail.repo, detail.release)
    if args.json:
        o.json({"schema_version": SCHEMA_VERSION, "repo": f"{host}:{detail.repo.slug}", **v.to_dict()})
        return EXIT_OK
    sym = {"ok": "+", "no": "x", "warn": "!", "info": "-"}
    o.out(f"{_cell(detail.repo.slug)}: {v.level.upper()} - {_cell(v.summary)}")
    for c in v.checks:
        o.out(f"  [{sym[c.status]}] {_cell(c.label)}: {_cell(c.detail)}")
    return EXIT_OK


def _rate_text(rate) -> str:
    return f"{rate.remaining}/{rate.limit}" if rate else "unknown"


def _cmd_accounts(args, hub, o: _Out) -> int:
    infos = asyncio.run(hub.accounts())
    if args.json:
        o.json({"schema_version": SCHEMA_VERSION, "accounts": [i.to_dict() for i in infos]})
    else:
        o.out(_table(["host", "status", "account", "token from", "scopes", "rate left", "star/fork"],
                     [[i.host, i.status, i.login or "-", i.source or "-",
                       ",".join(i.scopes) if i.scopes else ("unknown" if i.scopes is None else "none"),
                       _rate_text(i.rate), i.can_star_fork] for i in infos]))
        for i in infos:
            if i.message or i.hint:
                o.err(f"{_cell(i.host)}: {_msg(i.message or i.hint)}")
    good = [i for i in infos if i.status in ("signed in", "not signed in")]
    return EXIT_OK if len(good) == len(infos) else (EXIT_PARTIAL if good else EXIT_ERROR)


_COMMANDS = {"search": _cmd_search, "repo": _cmd_repo, "shelves": _cmd_shelves,
             "shelf": _cmd_shelf, "favorites": _cmd_favorites, "accounts": _cmd_accounts,
             "recommend": _cmd_recommend, "similar": _cmd_similar, "releases": _cmd_releases,
             "cloned": _cmd_cloned, "check": _cmd_check, "roots": _cmd_roots, "plan": _cmd_plan}


def main(argv: list[str] | None = None, *, hub_factory: Callable | None = None,
         stdout: TextIO | None = None, stderr: TextIO | None = None,
         gh_cli: Callable | None = None) -> int:
    stdout = stdout if stdout is not None else sys.stdout
    stderr = stderr if stderr is not None else sys.stderr
    # Parse first: --help/--version and usage errors must not build a hub or read tokens.
    parser = _build_parser()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        args = parser.parse_args(argv)  # exits (SystemExit) on --help/--version/usage error
    o = _Out(stdout, stderr)
    try:
        # Only now is the (small) hosts file read: the registry it defines is needed by --host,
        # repo validation and hosts. build_hub() configures again from the same file, which yields
        # an equal registry. This builds no providers, opens no database and uses no network.
        host_problems = configure_hosts()
        if args.command != "hosts":  # `hosts` prints them itself (also in its JSON)
            for p in host_problems[:MAX_PROBLEMS_SHOWN]:
                o.err(f"warning: {p}")
        if args.command == "search":
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                _check_host(parser, args.host)  # exits 2 on an unknown host
        if args.command == "hosts":
            return _cmd_hosts(args, None, o, host_problems, gh_cli)
        if args.command == "repo" and _valid_repo_arg(args.repo) is None:
            return _cmd_repo(args, None, o)
        if args.command == "similar" and _valid_repo_arg(args.repo) is None:
            return _cmd_similar(args, None, o)
        if args.command == "check" and _valid_repo_arg(args.repo) is None:
            return _cmd_check(args, None, o)
        if args.command == "cloned":
            return _cmd_cloned(args, None, o)  # no hub, no network
        if args.command == "roots":
            return _cmd_roots(args, None, o)  # no hub, no network
        if args.command == "plan":
            return _cmd_plan(args, None, o)  # no hub, no network, nothing run
        if hub_factory is None:
            from repohub.config import build_hub as hub_factory
        hub = hub_factory() if args.command != "shelves" else None
        return _COMMANDS[args.command](args, hub, o)
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        o.err(f"error: {type(e).__name__}")
        return EXIT_ERROR


def run() -> None:
    sys.exit(main())
