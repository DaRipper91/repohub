"""Extra Forgejo/Gitea hosts from a user-edited ``hosts.yaml``.

This file decides where requests go and which token is sent, so it is treated as untrusted.
Parsing uses ``yaml.safe_load`` only; the path is stat'ed first (never open a FIFO); the size
and entry counts are bounded; every entry is validated strictly and an invalid entry is skipped
with a problem. The built-in hosts ALWAYS load, whatever the file contains.
"""
from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from platformdirs import user_config_dir

from repohub.core.hosts import BUILTIN_HOSTS, ID_RE, RESERVED_IDS, HostRegistry, HostSpec, set_registry
from repohub.core.textsafe import clean_text

MAX_HOSTS_BYTES = 256 * 1024
MAX_HOST_ENTRIES = 20
MAX_NAME = 40
MAX_PATH_SHOWN = 200
_HOST_KEYS = ("id", "kind", "url", "token_env", "name")
# Extra hosts may only use REPOHUB_-prefixed variables, so a tampered hosts.yaml cannot bind an
# unrelated secret (AWS_SESSION_TOKEN, NPM_TOKEN, ...) to a host of its choosing.
TOKEN_PREFIX = "REPOHUB_"
_TOKEN_RE = re.compile(r"^REPOHUB_[A-Z][A-Z0-9_]{0,50}_TOKEN$")  # use fullmatch
_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


@dataclass
class LoadedHosts:
    registry: HostRegistry
    problems: list[str] = field(default_factory=list)


def personal_hosts_path() -> Path:
    return Path(user_config_dir("repohub")) / "hosts.yaml"


def _show(value: object) -> str:
    """Bounded, sanitised repr of untrusted text for error messages.

    Containers are described by type only: YAML aliases share objects, so ``str()`` of a
    small file can expand exponentially (an alias bomb).
    """
    if isinstance(value, str):
        return repr(clean_text(value)[:60])
    if value is None or isinstance(value, (bool, int, float)):
        return repr(value)[:60]
    return f"<{type(value).__name__}>"


def _problem_text(path: Path, err: object) -> str:
    return f"hosts file ({clean_text(str(path))[:MAX_PATH_SHOWN]}): {clean_text(str(err))[:300]}"


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys instead of silently keeping the last."""

    def construct_mapping(self, node, deep=False):
        if isinstance(node, yaml.MappingNode):
            self.flatten_mapping(node)
            seen = set()
            for key_node, _ in node.value:
                key = self.construct_object(key_node, deep=True)
                try:
                    if key in seen:
                        raise yaml.constructor.ConstructorError(
                            "while constructing a mapping", node.start_mark,
                            "found duplicate key", key_node.start_mark)
                    seen.add(key)
                except TypeError:
                    pass  # unhashable; the base class reports it
        return super().construct_mapping(node, deep=deep)


def _safe_load(text: str) -> object:
    try:
        return yaml.load(text, Loader=_UniqueKeyLoader)  # noqa: S506 (SafeLoader subclass)
    except yaml.YAMLError as e:
        raise ValueError(f"invalid YAML: {clean_text(str(e))[:200]}") from e
    except RecursionError as e:
        raise ValueError("invalid YAML: nesting too deep") from e


def _read_file(path: Path, limit: int) -> str:
    # Stat first: open() on a FIFO would block forever. stat follows symlinks.
    if not stat.S_ISREG(os.stat(path).st_mode):
        raise ValueError("not a regular file")
    with open(path, "rb") as fh:
        raw = fh.read(limit + 1)
    if len(raw) > limit:
        raise ValueError(f"file too large (over {limit} bytes)")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ValueError("file is not valid UTF-8") from e


def _parse_domain(url: object) -> str:
    """Validate an ``https://host[/]`` URL and return the lowercased hostname.

    Decision: IP literals and ``localhost`` are rejected. A token is sent to whatever this file
    names, so pointing a host at the loopback or a private address would let a config line
    reach local services (SSRF-style); real instances always have a public DNS name. Requiring
    a dotted name whose last label does not start with a digit rejects dotted-quad, hex, octal
    and integer forms; a bracketed IPv6 literal fails on the ``[`` character.
    """
    if not isinstance(url, str):
        raise ValueError("'url' must be a string")
    if not url or not url.isascii() or not url.isprintable() or " " in url:
        raise ValueError(f"'url' must be plain ASCII without spaces: {_show(url)}")
    if not url.lower().startswith("https://"):
        raise ValueError(f"'url' must start with https:// ({_show(url)})")
    rest = url[len("https://"):]
    if "\\" in rest:
        raise ValueError(f"'url' must not contain backslashes: {_show(url)}")
    for ch, what in (("?", "a query"), ("#", "a fragment"), (";", "params")):
        if ch in rest:
            raise ValueError(f"'url' must not contain {what}: {_show(url)}")
    host, slash, path = rest.partition("/")
    if path:
        raise ValueError(f"'url' must not contain a path: {_show(url)}")
    if "@" in host:
        raise ValueError(f"'url' must not contain credentials: {_show(url)}")
    if ":" in host:
        raise ValueError(f"'url' must not contain a port: {_show(url)}")
    host = host.lower()
    if not host:
        raise ValueError("'url' has no hostname")
    if len(host) > 253:
        raise ValueError("hostname is too long")
    labels = host.split(".")
    for label in labels:
        if not _LABEL_RE.fullmatch(label):
            raise ValueError(f"invalid hostname {_show(host)}")
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError("localhost is not allowed")
    if len(labels) < 2:
        raise ValueError(f"hostname needs a dot, like git.example.org: {_show(host)}")
    if labels[-1][0].isdigit():
        raise ValueError(f"IP addresses are not allowed, use a hostname: {_show(host)}")
    return host


def _parse_host(raw: object, taken_ids: set[str], taken_domains: set[str],
                taken_tokens: set[str]) -> HostSpec:
    if not isinstance(raw, dict):
        raise ValueError(f"expected a mapping, got {type(raw).__name__}")
    unknown = [k for k in raw if k not in _HOST_KEYS]
    if unknown:
        raise ValueError(f"unknown key(s) {', '.join(_show(k) for k in unknown)}")
    for key in ("id", "kind", "url"):
        if key not in raw:
            raise ValueError(f"missing required key {key!r}")
    host_id = raw["id"]
    # fullmatch, not match: the regexes end in '$', which also accepts a trailing newline.
    if not isinstance(host_id, str) or not ID_RE.fullmatch(host_id):
        raise ValueError(f"invalid id {_show(host_id)} (lowercase letters, digits and '-', "
                         "starting with a letter, at most 20 characters)")
    if host_id in RESERVED_IDS:
        raise ValueError(f"id {_show(host_id)} is reserved (it means every host in searches)")
    if host_id in taken_ids:
        raise ValueError(f"id {_show(host_id)} is already used")
    if raw["kind"] != "forgejo":
        raise ValueError("only kind 'forgejo' is supported for extra hosts")
    domain = _parse_domain(raw["url"])
    if domain in taken_domains:
        raise ValueError(f"host {_show(domain)} is already configured")
    name_raw = raw.get("name")
    if name_raw is None:
        name = host_id
    elif not isinstance(name_raw, str):
        raise ValueError("'name' must be a string")
    else:
        name = clean_text(name_raw)[:MAX_NAME] or host_id
    token = raw.get("token_env")
    if token is None:
        token = TOKEN_PREFIX + host_id.upper().replace("-", "_") + "_TOKEN"
        if token in taken_tokens:
            raise ValueError(f"default token variable {token} is already used by another "
                             "host; set 'token_env'")
    else:
        if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
            raise ValueError(f"invalid token_env {_show(token)}: the name must start with "
                             "REPOHUB_ and end in _TOKEN (capital letters, digits and '_'), "
                             "e.g. REPOHUB_MYFORGE_TOKEN")
        if token in taken_tokens:
            raise ValueError(f"token variable {token} is already used by another host")
    return HostSpec(host_id, "forgejo", name, domain, f"https://{domain}/api/v1", (token,), False)


def _parse_hosts(data: object) -> tuple[list[HostSpec], list[str]]:
    if data is None:
        return [], []
    if not isinstance(data, list):
        raise ValueError("file must contain a list")
    if len(data) > MAX_HOST_ENTRIES:
        raise ValueError(f"too many entries ({len(data)} > {MAX_HOST_ENTRIES})")
    taken_ids = {s.id for s in BUILTIN_HOSTS}
    taken_domains = {s.domain for s in BUILTIN_HOSTS}
    taken_tokens = {t for s in BUILTIN_HOSTS for t in s.token_env}
    specs: list[HostSpec] = []
    problems: list[str] = []
    for n, item in enumerate(data):
        try:
            spec = _parse_host(item, taken_ids, taken_domains, taken_tokens)
        except ValueError as e:
            problems.append(f"hosts file entry #{n}: {clean_text(str(e))[:300]}")
            continue
        taken_ids.add(spec.id)
        taken_domains.add(spec.domain)
        taken_tokens.update(spec.token_env)
        specs.append(spec)
    return specs, problems


def load_hosts(path: Path | str | None = None) -> LoadedHosts:
    """Built-ins plus valid extras. Never raises; does not change the active registry."""
    path = personal_hosts_path() if path is None else Path(path)  # "" is not "default"
    try:
        if path.is_symlink():
            try:
                os.stat(path)  # follows the link
            except OSError as e:  # dangling target or a loop
                raise ValueError("broken symlink") from e
        elif not path.exists():
            return LoadedHosts(HostRegistry(BUILTIN_HOSTS))
        specs, problems = _parse_hosts(_safe_load(_read_file(path, MAX_HOSTS_BYTES)))
        return LoadedHosts(HostRegistry(BUILTIN_HOSTS + tuple(specs)), problems)
    except Exception as e:  # a personal file must never break start-up
        return LoadedHosts(HostRegistry(BUILTIN_HOSTS), [_problem_text(path, e)])


def configure_hosts(path: Path | str | None = None) -> list[str]:
    """Load the hosts file, make it the active registry and return the problems."""
    loaded = load_hosts(path)
    set_registry(loaded.registry)
    return loaded.problems
