from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from typing import Callable, Mapping

from repohub.core.hosts import HostRegistry, registry


@dataclass(frozen=True)
class Tokens:
    github: str | None = field(default=None, repr=False)
    gitlab: str | None = field(default=None, repr=False)


def _gh_cli_token() -> str | None:
    try:
        r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    token = r.stdout.strip()
    return token if r.returncode == 0 and token else None


def find_tokens(env: Mapping[str, str] | None = None, gh_cli: Callable[[], str | None] = _gh_cli_token) -> Tokens:
    env = os.environ if env is None else env
    def clean(value: str | None) -> str | None:
        value = (value or "").strip()
        return value or None

    github = clean(env.get("GITHUB_TOKEN")) or clean(env.get("GH_TOKEN")) or clean(gh_cli())
    gitlab = clean(env.get("GITLAB_TOKEN"))
    return Tokens(github, gitlab)


class _Secret:
    """Holds one token value; every textual form is masked, so dumps of the owner cannot leak it."""

    __slots__ = ("_v",)

    def __init__(self, value: str):
        self._v = value

    def reveal(self) -> str:
        return self._v

    def __repr__(self) -> str:
        return "<hidden>"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("token values cannot be pickled")


@dataclass(frozen=True)
class HostTokens:
    """Token per host id. Values are reachable only through ``for_host``; repr/str/vars() hide them."""

    _secrets: Mapping[str, object] = field(default_factory=dict, repr=False)

    def __post_init__(self):
        wrapped = {h: (v if isinstance(v, _Secret) else _Secret(v))
                   for h, v in self._secrets.items() if v}
        object.__setattr__(self, "_secrets", wrapped)

    def for_host(self, host_id: str) -> str | None:
        secret = self._secrets.get(host_id)
        return secret.reveal() if secret is not None else None

    def has(self, host_id: str) -> bool:
        return host_id in self._secrets

    def __repr__(self) -> str:
        return f"HostTokens(hosts={sorted(self._secrets)})"

    __str__ = __repr__


def check_token_env_unique(reg: HostRegistry) -> None:
    """A token variable may belong to one host only: refuse to hand one secret to two hosts."""
    owner: dict[str, str] = {}
    for spec in reg.specs:
        for var in spec.token_env:
            if owner.setdefault(var, spec.id) != spec.id:
                raise ValueError(f"token variable {var} is listed by both {owner[var]!r} and {spec.id!r}")


def find_host_tokens(reg: HostRegistry | None = None, env: Mapping[str, str] | None = None,
                     gh_cli: Callable[[], str | None] = _gh_cli_token) -> HostTokens:
    """Per registered host: the first non-empty (stripped) value of its own token variables.

    Only variables named by a registered host are looked up, by exact name. The GitHub CLI is a
    fallback for the ``github`` host alone and is not called when an env value exists.
    """
    reg = registry() if reg is None else reg
    check_token_env_unique(reg)
    env = os.environ if env is None else env
    found: dict[str, str] = {}
    for spec in reg.specs:
        value = None
        for var in spec.token_env:
            value = (env.get(var) or "").strip() or None
            if value:
                break
        if value is None and spec.id == "github":
            value = (gh_cli() or "").strip() or None
        if value:
            found[spec.id] = value
    return HostTokens(found)
