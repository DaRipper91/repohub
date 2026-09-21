"""The proposed build steps for a cloned repository, from a FIXED table keyed by project type.

Nothing here comes from repository text, except one boolean: whether ``package.json`` has a ``build``
script (read with a size cap). Commands are argv tuples, never shell strings.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from repohub.core.detect import marker_files
from repohub.core.textsafe import clean_text

VENV = "@VENV@"  # replaced by the clone folder's .venv path when the step is run
MAX_PACKAGE_JSON = 256 * 1024
MAX_SCRIPT_SHOWN = 300
WARNING = ("This runs the repository's own build scripts on your machine with your permissions. "
           "RepoHub cannot sandbox it. Only continue for code you trust.")


@dataclass(frozen=True)
class Step:
    id: str
    title: str
    argv: tuple[str, ...]  # argv[0] is a tool name (or the @VENV@ marker) resolved when the step is run
    note: str = ""  # shown at approval and part of the digest; may quote repository text, which is only ever DISPLAYED

    def text(self) -> str:
        return " ".join(self.argv).replace(VENV, ".venv")


@dataclass(frozen=True)
class Plan:
    folder: str
    steps: tuple[Step, ...]

    @property
    def digest(self) -> str:
        """Identifies exactly what is being proposed for exactly this folder."""
        blob = json.dumps([self.folder, [(s.id, s.argv, s.note) for s in self.steps]], sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()

    def step(self, step_id: str) -> Step | None:
        return next((s for s in self.steps if s.id == step_id), None)


def _build_script(folder: Path) -> str | None:
    """The text of package.json's ``build`` script, for DISPLAY only (it is never used to form a command)."""
    pj = folder / "package.json"
    try:
        if pj.is_symlink() or pj.stat().st_size > MAX_PACKAGE_JSON:
            return None
        scripts = json.loads(pj.read_text(encoding="utf-8")).get("scripts")
    except (OSError, ValueError, AttributeError):
        return None
    text = scripts.get("build") if isinstance(scripts, dict) else None
    return clean_text(text)[:MAX_SCRIPT_SHOWN] if isinstance(text, str) else None


def build_plan(folder: Path | str, files: frozenset[str] | None = None,
               build_script: Callable[[Path], str | None] = _build_script) -> Plan:
    folder = Path(folder)
    files = marker_files(folder) if files is None else files
    steps: list[Step] = []
    primary = False
    if "Cargo.toml" in files:
        steps.append(Step("cargo-build", "Build with cargo (release)", ("cargo", "build", "--release")))
        primary = True
    if files & {"pyproject.toml", "setup.py", "requirements.txt"}:
        steps.append(Step("py-venv", "Create a virtual environment", ("python3", "-m", "venv", ".venv")))
        if files & {"pyproject.toml", "setup.py"}:
            steps.append(Step("py-install", "Install the project into it", (f"{VENV}/bin/pip", "install", ".")))
        else:
            steps.append(Step("py-install", "Install its requirements into it",
                              (f"{VENV}/bin/pip", "install", "-r", "requirements.txt")))
        primary = True
    if "package.json" in files:
        steps.append(Step("npm-install", "Install dependencies with npm", ("npm", "install"),
                          "npm also runs the install scripts of the packages it downloads."))
        script = build_script(folder)
        if script is not None:
            steps.append(Step("npm-build", "Run the project's build script", ("npm", "run", "build"),
                              f"The script it runs (from package.json): {script}"))
        primary = True
    if "go.mod" in files:
        steps.append(Step("go-build", "Build with go", ("go", "build", "./...")))
        primary = True
    if "CMakeLists.txt" in files:
        steps.append(Step("cmake-config", "Configure with cmake", ("cmake", "-S", ".", "-B", "build")))
        steps.append(Step("cmake-build", "Build with cmake", ("cmake", "--build", "build")))
        primary = True
    if "meson.build" in files:
        steps.append(Step("meson-setup", "Configure with meson", ("meson", "setup", "build")))
        steps.append(Step("meson-compile", "Build with meson", ("meson", "compile", "-C", "build")))
        primary = True
    if not primary and files & {"Makefile", "makefile", "GNUmakefile"}:
        steps.append(Step("make", "Build with make", ("make",)))
    return Plan(str(folder), tuple(steps))
