"""Find which repositories are already cloned, by reading only the clone folder.

Only the immediate subfolders of the clone folder are looked at, and only each one's ``.git/config``
(a regular file, never a symlink, size-capped) for the ``origin`` URL. Nothing is executed, no other file is
read, and nothing outside the clone folder is touched. The config text is untrusted: it is parsed with plain
string handling and the result must name a configured host and a valid repository name.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from repohub.core.hosts import registry

MAX_FOLDERS = 500
MAX_CONFIG_BYTES = 64 * 1024
MAX_URL = 300
_SCP = re.compile(r"^[A-Za-z0-9._-]+@([A-Za-z0-9.-]+):(.+)$")
_SECTION = re.compile(r'^\[\s*([A-Za-z0-9.-]+)(?:\s+"((?:[^"\\]|\\.)*)")?\s*\]\s*$')
_URL_LINE = re.compile(r"^url\s*=\s*(.*)$", re.I)


@dataclass(frozen=True)
class CloneInfo:
    host: str
    slug: str
    path: str

    @property
    def key(self) -> str:
        return f"{self.host}:{self.slug.lower()}"


def parse_remote(url: str) -> tuple[str, str] | None:
    """(host id, slug) for a remote URL on a configured host, else None. Credentials are ignored."""
    url = (url or "").strip().strip('"')
    if not url or len(url) > MAX_URL or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url):
        return None
    reg = registry()
    hostname, path = None, ""
    m = _SCP.match(url)
    if m and "://" not in url:
        hostname, path = m.group(1).lower(), m.group(2)
    else:
        try:
            u = urlparse(url)
            if u.scheme.lower() not in ("https", "http", "ssh", "git"):
                return None
            hostname, path = (u.hostname or "").lower(), u.path
        except ValueError:
            return None
    host = reg.id_for_domain(hostname or "")
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if host is None or not reg.slug_ok(host, path):
        return None
    return host, path


def _origin_url(config_text: str) -> str | None:
    in_origin = False
    for raw in config_text.splitlines():
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        sec = _SECTION.match(line)
        if sec:
            in_origin = sec.group(1).lower() == "remote" and sec.group(2) == "origin"
            continue
        if in_origin:
            m = _URL_LINE.match(line)
            if m:
                return m.group(1).strip()
    return None


def _read_config(folder: Path) -> str | None:
    git = folder / ".git"
    try:
        if git.is_symlink() or not git.is_dir():
            return None
        cfg = git / "config"
        if cfg.is_symlink():
            return None
        st = cfg.lstat()
        if not cfg.is_file() or st.st_size > MAX_CONFIG_BYTES:
            return None
        with open(cfg, "rb") as f:
            return f.read(MAX_CONFIG_BYTES).decode("utf-8", "replace")
    except OSError:
        return None


def scan_clones(root: Path | str) -> dict[str, CloneInfo]:
    """Map repository key -> where it is cloned, from the immediate subfolders of ``root``."""
    found: dict[str, CloneInfo] = {}
    try:
        base = Path(root).expanduser()
        entries = sorted(os.scandir(base), key=lambda e: e.name)
    except OSError:
        return found
    for count, entry in enumerate(entries):
        if count >= MAX_FOLDERS:
            break
        try:
            if entry.name.startswith(".") or entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        text = _read_config(Path(entry.path))
        url = _origin_url(text) if text else None
        parsed = parse_remote(url) if url else None
        if parsed:
            info = CloneInfo(parsed[0], parsed[1], entry.path)
            found.setdefault(info.key, info)  # the first folder (by name) wins
    return found
