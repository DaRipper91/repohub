from __future__ import annotations

import os
import re
import shutil
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


def _validate(url: str) -> tuple[str, str, str]:
    """Return (hostname, path, name) for a safe clone URL, or raise CloneError."""
    if not isinstance(url, str):
        raise CloneError("URL must be a string")
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url):
        raise CloneError("URL contains whitespace or control characters")
    u = urlparse(url)
    if u.scheme != "https" or u.hostname not in HOSTS.values() or u.username or u.password or u.port:
        raise CloneError("only plain https URLs on github.com or gitlab.com are allowed")
    if u.query or u.fragment or u.params or ":" in u.netloc:
        raise CloneError("only plain https URLs on github.com or gitlab.com are allowed")
    path = u.path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    host = "github" if u.hostname == "github.com" else "gitlab"
    if not valid_slug(path, host):
        raise CloneError("invalid repository path")
    name = path.split("/")[-1]
    if not _NAME.fullmatch(name):
        raise CloneError("invalid folder name")
    return u.hostname, path, name


def plan_clone(url: str, dest_root: Path | str) -> Path:
    _, _, name = _validate(url)
    root = Path(dest_root).expanduser().resolve()
    target = (root / name).resolve()
    if target.parent != root:
        raise CloneError("destination escapes the chosen folder")
    if target.exists():
        raise CloneError(f"{target} already exists")
    return target


def _last_line(stderr: str | None) -> str:
    lines = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
    line = lines[-1] if lines else "git clone failed"
    return re.sub(r"://[^/@\s]+@", "://***@", line)


def clone(url: str, dest_root: Path | str, shallow: bool = True, runner=subprocess.run) -> Path:
    hostname, path, _ = _validate(url)
    target = plan_clone(url, dest_root)
    canonical = f"https://{hostname}/{path}.git"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise CloneError("cannot create destination folder") from None
    args = ["git", "clone"] + (["--depth", "1"] if shallow else []) + ["--", canonical, str(target)]
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ALLOW_PROTOCOL": "https",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }
    try:
        r = runner(args, capture_output=True, text=True, timeout=600, env=env)
    except subprocess.TimeoutExpired:
        shutil.rmtree(target, ignore_errors=True)
        raise CloneError("git clone timed out") from None
    except OSError:
        shutil.rmtree(target, ignore_errors=True)
        raise CloneError("git could not be run") from None
    if r.returncode != 0:
        shutil.rmtree(target, ignore_errors=True)
        raise CloneError(_last_line(r.stderr))
    return target
