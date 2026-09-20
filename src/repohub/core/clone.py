from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from repohub.core.providers.base import valid_slug

HOSTS = {"github": "github.com", "gitlab": "gitlab.com"}
_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")


class CloneError(Exception):
    pass


def clone_url(host: str, slug: str) -> str:
    if host not in HOSTS or not valid_slug(slug, host):
        raise CloneError("invalid repository")
    return f"https://{HOSTS[host]}/{slug}.git"


def plan_clone(url: str, dest_root: Path | str) -> Path:
    u = urlparse(url)
    if u.scheme != "https" or u.hostname not in HOSTS.values() or u.username or u.password or u.port:
        raise CloneError("only plain https URLs on github.com or gitlab.com are allowed")
    path = u.path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    host = "github" if u.hostname == "github.com" else "gitlab"
    if not valid_slug(path, host):
        raise CloneError("invalid repository path")
    name = path.split("/")[-1]
    if not _NAME.match(name):
        raise CloneError("invalid folder name")
    root = Path(dest_root).expanduser().resolve()
    target = (root / name).resolve()
    if target.parent != root:
        raise CloneError("destination escapes the chosen folder")
    if target.exists():
        raise CloneError(f"{target} already exists")
    return target


def clone(url: str, dest_root: Path | str, shallow: bool = True, runner=subprocess.run) -> Path:
    target = plan_clone(url, dest_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    args = ["git", "clone"] + (["--depth", "1"] if shallow else []) + ["--", url, str(target)]
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    r = runner(args, capture_output=True, text=True, timeout=600, env=env)
    if r.returncode != 0:
        raise CloneError((r.stderr or "git clone failed").strip().splitlines()[-1])
    return target
