"""Find which repositories are already cloned, by reading only the clone folder.

Only the immediate subfolders of the clone folder are looked at, and only each one's ``.git/config``
(a regular file, never a symlink, size-capped) for the ``origin`` URL. Nothing is executed, no other file is
read, and nothing outside the clone folder is touched. The config text is untrusted: it is parsed with plain
string handling and the result must name a configured host and a valid repository name.
"""
from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from repohub.core.hosts import registry

MAX_FOLDERS = 500
MAX_LISTED = 5000  # directory entries examined at most
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
    if not url or len(url) > MAX_URL or "\\" in url or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url):
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
        # O_NOFOLLOW/O_NONBLOCK: a config swapped for a symlink or FIFO after the checks cannot hang or redirect us
        fd = os.open(cfg, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_CONFIG_BYTES:
                return None
            return os.read(fd, MAX_CONFIG_BYTES).decode("utf-8", "replace")
        finally:
            os.close(fd)
    except OSError:
        return None


def scan_clones(root: Path | str) -> dict[str, CloneInfo]:
    """Map repository key -> where it is cloned, from the immediate subfolders of ``root``."""
    found: dict[str, CloneInfo] = {}
    folders: list[os.DirEntry] = []
    try:
        with os.scandir(Path(root).expanduser()) as it:
            for n, entry in enumerate(it):
                if n >= MAX_LISTED:
                    break
                try:
                    if not entry.name.startswith(".") and not entry.is_symlink() and entry.is_dir(follow_symlinks=False):
                        folders.append(entry)
                except OSError:
                    continue
    except OSError:
        return found
    for entry in sorted(folders, key=lambda e: e.name)[:MAX_FOLDERS]:
        text = _read_config(Path(entry.path))
        url = _origin_url(text) if text else None
        parsed = parse_remote(url) if url else None
        if parsed:
            info = CloneInfo(parsed[0], parsed[1], entry.path)
            found.setdefault(info.key, info)  # the first folder (by name) wins
    return found
