from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from typing import Callable, Mapping


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
