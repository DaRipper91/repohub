"""Ties the clone scan, project detector, machine facts and verdict together for the apps."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

from repohub.core.clonescan import CloneInfo, scan_clones
from repohub.core.detect import detect_project
from repohub.core.machine import Machine, read_machine
from repohub.core.models import Release, Repo
from repohub.core.runcheck import Verdict, run_check

SCAN_TTL = 10.0


class Awareness:
    def __init__(self, clone_root: Path | str, machine: Machine | None = None, *,
                 scan: Callable = scan_clones, clock: Callable[[], float] = time.monotonic,
                 extra_roots: Callable[[], list] | None = None):
        self.clone_root, self._scan, self._clock = clone_root, scan, clock
        self._extra = extra_roots
        self._machine = machine
        self._cache: tuple[float, dict[str, CloneInfo]] | None = None

    @property
    def machine(self) -> Machine:
        if self._machine is None:
            self._machine = read_machine()
        return self._machine

    def cloned(self, refresh: bool = False) -> dict[str, CloneInfo]:
        """Repository key -> clone. Rescanned at most every few seconds; never raises."""
        now = self._clock()
        if refresh or self._cache is None or now - self._cache[0] > SCAN_TTL:
            found: dict[str, CloneInfo] = {}
            for root in self.roots():
                try:
                    for key, info in self._scan(root).items():
                        found.setdefault(key, info)  # the clone folder first, then picked folders in order
                except Exception:
                    continue
            self._cache = (now, found)
        return self._cache[1]

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
        self._cache = None

    def clone_of(self, repo: Repo) -> CloneInfo | None:
        return self.cloned().get(repo.key)

    def check(self, repo: Repo, release: Release | None) -> Verdict:
        clone = self.clone_of(repo)
        detected = detect_project(clone.path) if clone else []
        return run_check(repo, release, self.machine, clone, detected)
