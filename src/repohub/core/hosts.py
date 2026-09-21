from __future__ import annotations

import re
from dataclasses import dataclass, field

KINDS = ("github", "gitlab", "forgejo")
RESERVED_IDS = ("all", "both")  # host: values that mean "every host"
ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,19}$")
DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$")  # lowercase; use fullmatch


@dataclass(frozen=True)
class HostSpec:
    id: str
    kind: str
    name: str
    domain: str
    api_base: str
    token_env: tuple[str, ...] = ()
    builtin: bool = False

    @property
    def web_base(self) -> str:
        return f"https://{self.domain}"


BUILTIN_HOSTS = (
    HostSpec("github", "github", "GitHub", "github.com", "https://api.github.com", ("GITHUB_TOKEN", "GH_TOKEN"), True),
    HostSpec("gitlab", "gitlab", "GitLab", "gitlab.com", "https://gitlab.com/api/v4", ("GITLAB_TOKEN",), True),
    HostSpec("codeberg", "forgejo", "Codeberg", "codeberg.org", "https://codeberg.org/api/v1", ("CODEBERG_TOKEN",), True),
)


class HostRegistry:
    """An ordered set of hosts. The first host wins ties when results are merged."""

    def __init__(self, specs):
        self._specs: dict[str, HostSpec] = {}
        self._domains: dict[str, str] = {}
        for spec in specs:
            if spec.kind not in KINDS:
                raise ValueError(f"unknown host kind {spec.kind!r}")
            if not ID_RE.fullmatch(spec.id):  # fullmatch: '$' would accept a trailing newline
                raise ValueError(f"invalid host id {spec.id!r}")
            if not DOMAIN_RE.fullmatch(spec.domain):
                raise ValueError(f"invalid host domain {spec.domain!r}")
            if spec.id in RESERVED_IDS:
                raise ValueError(f"host id {spec.id!r} is reserved")
            if spec.id in self._specs:
                raise ValueError(f"duplicate host id {spec.id!r}")
            if spec.domain in self._domains:
                raise ValueError(f"duplicate host domain {spec.domain!r}")
            self._specs[spec.id] = spec
            self._domains[spec.domain] = spec.id

    @property
    def specs(self) -> tuple[HostSpec, ...]:
        return tuple(self._specs.values())

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(self._specs)

    def __contains__(self, host_id: object) -> bool:
        return host_id in self._specs

    def get(self, host_id: str) -> HostSpec | None:
        return self._specs.get(host_id)

    def rank(self, host_id: str) -> int:
        """Position in the registry (used to break ties); unknown hosts sort last."""
        return self.ids.index(host_id) if host_id in self._specs else len(self._specs)

    def id_for_domain(self, domain: str) -> str | None:
        return self._domains.get(domain.lower())

    def clone_domains(self) -> dict[str, str]:
        return {s.id: s.domain for s in self._specs.values()}

    def slug_ok(self, host_id: str, slug: str) -> bool:
        """Slug shape for a configured host: owner/name for github and forgejo, nested groups for gitlab."""
        from repohub.core.providers.base import valid_slug

        spec = self._specs.get(host_id)
        if spec is None:
            return False
        return valid_slug(slug, "gitlab" if spec.kind == "gitlab" else "github")


_active = HostRegistry(BUILTIN_HOSTS)


def registry() -> HostRegistry:
    return _active


def set_registry(reg: HostRegistry) -> None:
    global _active
    _active = reg


def reset_registry() -> None:
    set_registry(HostRegistry(BUILTIN_HOSTS))
