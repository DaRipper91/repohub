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

from repohub.core.browse import Shelf, load_all_shelves
from repohub.core.models import HOSTS, MAX_DAYS, MAX_STARS, SORTS, Repo, SearchFilters
from repohub.core.providers.base import NotFound, ProviderError, valid_slug
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
    try:
        from importlib.metadata import version
        return f"repohub {version('repohub')}"
    except Exception:
        return "repohub"


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="repohub", description="Search GitHub and GitLab repositories (read-only).")
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
    s.add_argument("--host", choices=("github", "gitlab", "both"), default=None)
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
    fv.add_argument("--json", action="store_true")
    return p


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
    rows = [[_fit(r.slug, 60), _fit(r.host, 8), str(r.stars), _fit(r.language, 20), _date(r.pushed_at), _short(r.description)] for r in repos]
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
                         hosts=HOSTS if args.host in (None, "both") else (args.host,),
                         include_archived=args.archived, sort=args.sort or "stars",
                         hide_forks=args.no_forks)
    parsed = parse_query(" ".join(args.text), base)
    o.problems(parsed.problems)
    result = asyncio.run(hub.search(parsed.text, parsed.filters))
    return _emit_list(o, args.json, result.repos[:args.limit], dict(result.errors), result.stale)


def _valid_repo_arg(value: str) -> tuple[str, str] | None:
    host, sep, slug = value.partition(":")
    if not sep or host not in HOSTS or len(slug) > 200 or not valid_slug(slug, host):
        return None
    return host, slug


def _cmd_repo(args, hub, o: _Out) -> int:
    parsed = _valid_repo_arg(args.repo)
    if parsed is None:
        o.err("error: repository must look like github:owner/name or gitlab:group/name")
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
    return _emit_list(o, args.json, hub.favorites.list(), {}, False)


_COMMANDS = {"search": _cmd_search, "repo": _cmd_repo, "shelves": _cmd_shelves,
             "shelf": _cmd_shelf, "favorites": _cmd_favorites}


def main(argv: list[str] | None = None, *, hub_factory: Callable | None = None,
         stdout: TextIO | None = None, stderr: TextIO | None = None) -> int:
    stdout = stdout if stdout is not None else sys.stdout
    stderr = stderr if stderr is not None else sys.stderr
    # Parse first: --help/--version and usage errors must not build a hub or read tokens.
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        args = _build_parser().parse_args(argv)  # exits (SystemExit) on --help/--version/usage error
    o = _Out(stdout, stderr)
    try:
        if args.command == "repo" and _valid_repo_arg(args.repo) is None:
            return _cmd_repo(args, None, o)  # rejected before any hub is built
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
