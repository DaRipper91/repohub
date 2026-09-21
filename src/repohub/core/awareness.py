"""Ties the clone scan, project detector, machine facts and verdict together for the apps."""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

from repohub.core.clonescan import CloneInfo, scan_clones
from repohub.core.detect import detect_project
from repohub.core.machine import Machine, read_machine
from repohub.core.models import Release, Repo
from repohub.core.runcheck import Verdict, run_check

SCAN_TTL = 10.0
SCAN_WAIT = 1.5  # seconds a caller waits for a scan; a dead or slow folder never blocks longer


class Awareness:
    def __init__(self, clone_root: Path | str, machine: Machine | None = None, *,
                 scan: Callable = scan_clones, clock: Callable[[], float] = time.monotonic,
                 extra_roots: Callable[[], list] | None = None):
        self.clone_root, self._scan, self._clock = clone_root, scan, clock
        self._extra = extra_roots
        self._machine = machine
        self._cache: tuple[float, dict[str, CloneInfo]] | None = None
        self._lock = threading.Lock()
        self._gen = 0  # bumped by invalidate(): a scan started before it must not be stored
        self._worker: threading.Thread | None = None
        self._done = threading.Event()

    @property
    def machine(self) -> Machine:
        if self._machine is None:
            self._machine = read_machine()
        return self._machine

    def cloned(self, refresh: bool = False) -> dict[str, CloneInfo]:
        """Repository key -> clone. One background scan at a time; callers wait at most SCAN_WAIT seconds.

        If a folder is slow or unreachable the last result (or nothing) is returned instead of hanging.
        """
        with self._lock:
            fresh = self._cache is not None and self._clock() - self._cache[0] <= SCAN_TTL
            if fresh and not refresh:
                return self._cache[1]
            if self._worker is None or not self._worker.is_alive():
                self._done.clear()
                self._worker = threading.Thread(target=self._refresh, name="repohub-scan", daemon=True)
                self._worker.start()
        self._done.wait(SCAN_WAIT)
        with self._lock:
            return self._cache[1] if self._cache is not None else {}

    def _refresh(self) -> None:
        try:
            while True:
                with self._lock:
                    gen = self._gen
                found: dict[str, CloneInfo] = {}
                for root in self.roots():
                    try:
                        for key, info in self._scan(root).items():
                            found.setdefault(key, info)  # the clone folder first, then picked folders in order
                    except Exception:
                        continue
                with self._lock:
                    if gen == self._gen:
                        self._cache = (self._clock(), found)
                        return
        finally:
            self._done.set()

    def roots(self) -> list:
        """The clone folder, then any extra folders the user picked."""
        extra: list = []
        if self._extra is not None:
            try:
                extra = [r for r in self._extra() if str(r) != str(self.clone_root)]
            except Exception:
                extra = []
        return [self.clone_root, *extra]

    def invalidate(self) -> None:
        with self._lock:
            self._gen += 1
            self._cache = None

    def clone_of(self, repo: Repo) -> CloneInfo | None:
        return self.cloned().get(repo.key)

    def check(self, repo: Repo, release: Release | None) -> Verdict:
        clone = self.clone_of(repo)
        detected = detect_project(clone.path) if clone else []
        return run_check(repo, release, self.machine, clone, detected)
