"""What kind of project a folder is, from the names of its top-level files only. Nothing is read or run."""
from __future__ import annotations

import os
from pathlib import Path

MARKERS = {
    "Cargo.toml": "rust", "pyproject.toml": "python", "requirements.txt": "python", "setup.py": "python",
    "setup.cfg": "python", "package.json": "node", "go.mod": "go", "Makefile": "make", "makefile": "make",
    "GNUmakefile": "make", "CMakeLists.txt": "cmake", "meson.build": "meson", "Dockerfile": "docker",
    "pom.xml": "java", "build.gradle": "java", "build.gradle.kts": "java", "Gemfile": "ruby",
}
MAX_ENTRIES = 2000


def detect_project(folder: Path | str) -> list[str]:
    """Project kinds found among the top-level names of ``folder`` (stable order, no duplicates)."""
    kinds: list[str] = []
    try:
        with os.scandir(folder) as it:
            for n, entry in enumerate(it):
                if n >= MAX_ENTRIES:
                    break
                kind = MARKERS.get(entry.name)
                if kind and kind not in kinds:
                    kinds.append(kind)
    except OSError:
        return []
    return sorted(kinds)
