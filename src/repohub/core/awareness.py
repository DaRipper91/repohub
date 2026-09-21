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
                 scan: Callable = scan_clones, clock: Callable[[], float] = time.monotonic):
        self.clone_root, self._scan, self._clock = clone_root, scan, clock
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
            try:
                found = self._scan(self.clone_root)
            except Exception:
                found = {}
            self._cache = (now, found)
        return self._cache[1]

    def clone_of(self, repo: Repo) -> CloneInfo | None:
        return self.cloned().get(repo.key)

    def check(self, repo: Repo, release: Release | None) -> Verdict:
        clone = self.clone_of(repo)
        detected = detect_project(clone.path) if clone else []
        return run_check(repo, release, self.machine, clone, detected)
