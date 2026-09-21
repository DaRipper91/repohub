from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from urllib.parse import urlparse

from repohub.core.hosts import registry
_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")


_IN_FLIGHT: set[Path] = set()
_LOCK = threading.Lock()


class CloneError(Exception):
    pass


def clone_url(host: str, slug: str) -> str:
    reg = registry()
    domain = reg.clone_domains().get(host)
    if domain is None or not reg.slug_ok(host, slug):
        raise CloneError("invalid repository")
    return f"https://{domain}/{slug}.git"


def _validate(url: str) -> tuple[str, str, str]:
    """Return (hostname, path, name) for a safe clone URL, or raise CloneError."""
    if not isinstance(url, str):
        raise CloneError("URL must be a string")
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url):
        raise CloneError("URL contains whitespace or control characters")
    reg = registry()
    domains = set(reg.clone_domains().values())  # exact (lowercase) match, never a suffix or substring
    try:  # urlparse and the .hostname/.port properties raise ValueError on malformed input
        u = urlparse(url)
        hostname = u.hostname
        bad = u.scheme != "https" or hostname not in domains or u.username or u.password or u.port
    except ValueError:
        raise CloneError("only plain https URLs on a configured host are allowed") from None
    if bad:
        raise CloneError("only plain https URLs on a configured host are allowed")
    if u.query or u.fragment or u.params or ":" in u.netloc:
        raise CloneError("only plain https URLs on a configured host are allowed")
    path = u.path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    host = reg.id_for_domain(hostname)
    if host is None or not reg.slug_ok(host, path):
        raise CloneError("invalid repository path")
    name = path.split("/")[-1]
    if not _NAME.fullmatch(name):
        raise CloneError("invalid folder name")
    return hostname, path, name


def plan_clone(url: str, dest_root: Path | str) -> Path:
    _, _, name = _validate(url)
    return _target_for(name, dest_root)


def _target_for(name: str, dest_root: Path | str) -> Path:
    """Destination folder for an already-validated folder name."""
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
    hostname, path, _name = _validate(url)
    target = _target_for(_name, dest_root)  # one validation only: everything derives from it
    canonical = f"https://{hostname}/{path}.git"
    with _LOCK:
        if target in _IN_FLIGHT:
            raise CloneError("a clone of this repository is already in progress")
        _IN_FLIGHT.add(target)
    try:
        if target.exists():  # re-check under our claim: never clean up a folder we did not create
            raise CloneError(f"{target} already exists")
        return _run_clone(canonical, target, shallow, runner)
    finally:
        with _LOCK:
            _IN_FLIGHT.discard(target)


def _run_clone(canonical: str, target: Path, shallow: bool, runner) -> Path:
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
