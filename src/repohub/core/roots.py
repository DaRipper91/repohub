"""Extra clone folders the user picked, and a bounded, user-triggered scan that suggests them.

Nothing is scanned unless the user asks. A scan walks folders (never following symlinks, never
entering hidden or build folders or system paths), and only reports folders that directly contain
git clones whose origin is on a configured host. Results are held in memory; only the folders the user
picks from those results are saved (paths only) in ``scan_roots.json`` in the config folder.
"""
from __future__ import annotations

import json
import os
import stat
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from platformdirs import user_config_dir

from repohub.core.clonescan import _origin_url, _read_config, parse_remote
from repohub.core.textsafe import clean_text

MAX_ROOTS = 20
MAX_FILE_BYTES = 64 * 1024
MAX_PATH = 4096
MAX_DEPTH = 6
MAX_DIRS = 100_000
BUDGET_SECONDS = 30.0
MAX_CANDIDATES = 200
# never scanned or saved: pseudo filesystems and system trees
FORBIDDEN = ("/proc", "/sys", "/dev", "/run", "/boot", "/snap", "/usr", "/lib", "/lib64", "/bin", "/sbin", "/etc",
             "/var/lib", "/var/cache", "/var/log", "/var/run", "/var/spool", "/var/tmp")  # /var/home stays allowed
SKIP_NAMES = frozenset({"node_modules", "__pycache__", "site-packages", "venv", "target", "build", "dist",
                        "vendor", "Trash", "lost+found"})


def _forbidden(path: str) -> bool:
    return path == "/" or any(path == f or path.startswith(f + "/") for f in FORBIDDEN)


def valid_root(value: object) -> Path | None:
    """A resolved, existing folder that is safe to use as a clone folder, else None."""
    if not isinstance(value, str) or not value or len(value) > MAX_PATH or "\x00" in value:
        return None
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        return None
    try:
        p = Path(value).expanduser()
        if not p.is_absolute():
            return None
        r = p.resolve()
        if not r.is_dir() or _forbidden(str(r)):
            return None
    except (OSError, RuntimeError):
        return None
    return r


def roots_path() -> Path:
    return Path(user_config_dir("repohub")) / "scan_roots.json"


class ScanRoots:
    """The saved extra clone folders. Every read re-validates; a damaged file yields no roots."""

    def __init__(self, path: Path | str | None = None):
        self._path = Path(path) if path is not None else None

    @property
    def path(self) -> Path:
        return self._path if self._path is not None else roots_path()

    def list(self) -> list[str]:
        try:
            st = self.path.lstat()
            if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_FILE_BYTES:
                return []
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        raw = data.get("roots") if isinstance(data, dict) else None
        out: list[str] = []
        for item in (raw if isinstance(raw, list) else [])[:MAX_ROOTS]:
            r = valid_root(item)
            if r is not None and str(r) not in out:
                out.append(str(r))
        return out

    def _write(self, roots: list[str]) -> None:
        target = self.path
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".scan_roots.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"roots": roots}, f, indent=2)
            os.replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def add(self, values) -> list[str]:
        """Save valid folders (at most MAX_ROOTS in total). Returns the folders that were added."""
        current = self.list()
        added: list[str] = []
        for v in values:
            r = valid_root(v)
            if r is None or str(r) in current or len(current) >= MAX_ROOTS:
                continue
            current.append(str(r))
            added.append(str(r))
        if added:
            self._write(current)
        return added

    def remove(self, value: str) -> bool:
        current = self.list()
        if value not in current:
            return False
        current.remove(value)
        self._write(current)
        return True


@dataclass(frozen=True)
class Candidate:
    path: str
    repos: int
    examples: tuple[str, ...] = ()


@dataclass
class ScanReport:
    candidates: list[Candidate] = field(default_factory=list)
    dirs_seen: int = 0
    truncated: bool = False
    seconds: float = 0.0
    start: str = ""


def discover(start: Path | str, *, max_depth: int = MAX_DEPTH, max_dirs: int = MAX_DIRS,
             budget: float = BUDGET_SECONDS, clock: Callable[[], float] = time.monotonic) -> ScanReport:
    """Find folders that directly contain recognised git clones, below ``start``.

    A folder counts as a clone when it has a real ``.git`` folder whose origin URL is on a configured host.
    Clones are not entered. Symlinks, hidden folders, build folders and system paths are never entered.
    """
    t0 = clock()
    report = ScanReport(start=str(start))
    stack: list[tuple[str, int]] = [(str(start), 0)]
    found: dict[str, list[str]] = {}
    while stack:
        if report.dirs_seen >= max_dirs or clock() - t0 > budget:
            report.truncated = True
            break
        path, depth = stack.pop()
        if _forbidden(path):
            continue
        report.dirs_seen += 1
        try:
            with os.scandir(path) as it:
                entries = list(it)
        except OSError:
            continue
        names = {e.name for e in entries}
        if ".git" in names:
            text = _read_config(Path(path))
            url = _origin_url(text) if text else None
            if url and parse_remote(url):
                found.setdefault(str(Path(path).parent), []).append(Path(path).name)
                continue  # a recognised clone is never descended into
            # a git folder that is not a recognised clone (a home folder kept in git, say) is walked as usual
        if depth >= max_depth:
            continue
        for e in entries:
            try:
                if (e.name.startswith(".") or e.name in SKIP_NAMES or e.is_symlink()
                        or not e.is_dir(follow_symlinks=False)):
                    continue
            except OSError:
                continue
            stack.append((e.path, depth + 1))
    ranked = sorted(found.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:MAX_CANDIDATES]
    report.candidates = [Candidate(p, len(n), tuple(sorted(clean_text(x) for x in n)[:3])) for p, n in ranked
                         if valid_root(p) is not None]
    report.seconds = round(clock() - t0, 2)
    return report


def home_start() -> Path:
    return Path.home()
