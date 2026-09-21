from __future__ import annotations

import os
from pathlib import Path

from platformdirs import user_data_dir

from repohub.core.auth import HostTokens, check_token_env_unique, find_host_tokens
from repohub.core.cache import Cache
from repohub.core.hosts import HostRegistry, registry
from repohub.core.hostsconfig import configure_hosts
from repohub.core.hub import Hub
from repohub.core.providers.forgejo import ForgejoProvider
from repohub.core.providers.github import GitHubProvider
from repohub.core.providers.gitlab import GitLabProvider
from repohub.core.store import Favorites


def clone_root() -> Path:
    return Path(os.environ.get("REPOHUB_CLONE_DIR", "~/playground")).expanduser()


def make_providers(reg: HostRegistry | None, tokens: HostTokens) -> dict:
    """One provider per registered host, each given only its own host's token."""
    reg = registry() if reg is None else reg
    check_token_env_unique(reg)
    providers: dict = {}
    for spec in reg.specs:
        token = tokens.for_host(spec.id)
        if spec.kind == "github":
            providers[spec.id] = GitHubProvider(token)
        elif spec.kind == "gitlab":
            providers[spec.id] = GitLabProvider(token)
        elif spec.kind == "forgejo":
            providers[spec.id] = ForgejoProvider(spec.id, spec.api_base, token)
        else:
            raise ValueError(f"unknown host kind {spec.kind!r}")
    return providers


def build_hub() -> Hub:
    data = Path(user_data_dir("repohub"))
    data.mkdir(parents=True, exist_ok=True)
    db = str(data / "repohub.db")
    problems = configure_hosts()
    reg = registry()
    providers = make_providers(reg, find_host_tokens(reg))
    return Hub(providers, Cache(db), Favorites(db), host_problems=problems)
