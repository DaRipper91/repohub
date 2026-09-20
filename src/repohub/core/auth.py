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
    except (OSError, subprocess.SubprocessError):
        return None
    token = r.stdout.strip()
    return token if r.returncode == 0 and token else None


def find_tokens(env: Mapping[str, str] | None = None, gh_cli: Callable[[], str | None] = _gh_cli_token) -> Tokens:
    env = os.environ if env is None else env
    github = env.get("GITHUB_TOKEN") or env.get("GH_TOKEN") or gh_cli()
    gitlab = env.get("GITLAB_TOKEN")
    return Tokens(github or None, gitlab or None)
