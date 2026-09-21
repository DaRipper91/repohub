"""Facts about this machine: CPU architecture, memory and which build tools are on PATH.

Only ``shutil.which`` is used to look for tools; no tool is ever started.
"""
from __future__ import annotations

import platform
import shutil
from dataclasses import dataclass, field
from typing import Callable

# what building each kind of project usually needs (any one of a tuple's alternatives is enough)
TOOLS: dict[str, tuple[tuple[str, ...], ...]] = {
    "rust": (("cargo",),), "python": (("python3", "python"),), "node": (("node",), ("npm", "pnpm", "yarn")),
    "go": (("go",),), "make": (("make",),), "cmake": (("cmake",), ("make", "ninja")),
    "meson": (("meson",), ("ninja",)), "docker": (("docker", "podman"),), "java": (("java",),),
    "ruby": (("ruby",),), "c": (("gcc", "cc", "clang"), ("make", "cmake")),
}
LANGUAGE_KINDS = {
    "rust": "rust", "python": "python", "javascript": "node", "typescript": "node", "go": "go", "java": "java",
    "kotlin": "java", "c": "c", "c++": "c", "ruby": "ruby",
}


def arch_tag(machine: str) -> str:
    m = (machine or "").lower()
    if m in ("aarch64", "arm64") or m.startswith("armv8"):
        return "arm64"
    if m in ("x86_64", "amd64", "x64"):
        return "x86_64"
    return m or "unknown"


@dataclass(frozen=True)
class Machine:
    arch: str
    system: str
    ram_gb: float | None
    tools: frozenset = field(default_factory=frozenset)  # names found on PATH

    def has(self, alternatives: tuple[str, ...]) -> bool:
        return any(t in self.tools for t in alternatives)


def _ram_gb() -> float | None:
    try:
        with open("/proc/meminfo", "rb") as f:
            for line in f.read(4096).decode("ascii", "ignore").splitlines():
                if line.startswith("MemTotal:"):
                    return round(int(line.split()[1]) / 1024 / 1024, 1)
    except (OSError, ValueError, IndexError):
        pass
    return None


def read_machine(which: Callable[[str], str | None] = shutil.which) -> Machine:
    names = {t for groups in TOOLS.values() for alts in groups for t in alts} | {"git"}
    return Machine(arch_tag(platform.machine()), platform.system() or "unknown", _ram_gb(),
                   frozenset(n for n in names if which(n)))
