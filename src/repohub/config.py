from __future__ import annotations

import os
from pathlib import Path

from platformdirs import user_data_dir

from repohub.core.auth import find_tokens
from repohub.core.cache import Cache
from repohub.core.hub import Hub
from repohub.core.providers.github import GitHubProvider
from repohub.core.providers.gitlab import GitLabProvider
from repohub.core.store import Favorites


def clone_root() -> Path:
    return Path(os.environ.get("REPOHUB_CLONE_DIR", "~/playground")).expanduser()


def build_hub() -> Hub:
    data = Path(user_data_dir("repohub"))
    data.mkdir(parents=True, exist_ok=True)
    db = str(data / "repohub.db")
    tokens = find_tokens()
    providers = {"github": GitHubProvider(tokens.github), "gitlab": GitLabProvider(tokens.gitlab)}
    return Hub(providers, Cache(db), Favorites(db))
