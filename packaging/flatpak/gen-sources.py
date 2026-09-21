#!/usr/bin/env python3
"""Write the Flatpak module that installs RepoHub's Python dependencies from ready-made wheels.

    gen-sources.py OUTFILE [--python 3.13]

Resolves the dependency versions with uv, then asks PyPI for the wheel of each package (pure wheels for all
architectures, compiled ones for aarch64 and x86_64) and records URL and sha256, so the Flatpak build needs
no network access except for those pinned files.
"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys
import urllib.request

REPO = pathlib.Path(__file__).resolve().parents[2]

ARCHES = {"aarch64": "aarch64", "x86_64": "x86_64"}


def resolve(py: str) -> list[tuple[str, str]]:
    out = subprocess.run(["uv", "pip", "compile", "pyproject.toml", "--python-version", py, "--python-platform",
                          "x86_64-manylinux_2_28", "-q", "--no-header", "--no-annotate"],
                         capture_output=True, text=True, check=True, cwd=REPO).stdout
    pins = []
    for line in out.splitlines():
        m = re.match(r"^([A-Za-z0-9_.-]+)==([^\s;]+)", line.strip())
        if m:
            pins.append((m.group(1), m.group(2)))
    return pins


def files(name: str, version: str) -> list[dict]:
    with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/{version}/json", timeout=30) as r:
        return [f for f in json.load(r)["urls"] if f["packagetype"] == "bdist_wheel"]


def pick(fs: list[dict], py: str, arch: str | None) -> dict | None:
    tag = "cp" + py.replace(".", "")
    best = None
    for f in fs:
        name = f["filename"]
        if arch is None:
            if name.endswith("-none-any.whl"):
                return f
            continue
        if not re.search(rf"manylinux[^-]*_{arch}", name):
            continue
        exact = f"-{tag}-{tag}-" in name  # cp313-cp313, but NOT cp313-cp313t (free-threaded builds)
        if exact or "-abi3-" in name:
            # an exact match for this Python beats a stable-ABI wheel; among equals the newest manylinux tag wins
            if best is None or (exact, name) > (f"-{tag}-{tag}-" in best["filename"], best["filename"]):
                best = f
    return best


def main() -> None:
    out = sys.argv[1]
    py = sys.argv[sys.argv.index("--python") + 1] if "--python" in sys.argv else "3.13"
    sources, names = [], []
    for name, version in resolve(py):
        fs = files(name, version)
        pure = pick(fs, py, None)
        if pure:
            sources.append({"type": "file", "url": pure["url"], "sha256": pure["digests"]["sha256"]})
            names.append(pure["filename"])
            continue
        for arch in ARCHES:
            f = pick(fs, py, arch)
            if f is None:
                sys.exit(f"no {arch} wheel for {name}=={version} on Python {py}")
            sources.append({"type": "file", "url": f["url"], "sha256": f["digests"]["sha256"], "only-arches": [arch]})
    module = {"name": "python-deps", "buildsystem": "simple",
              "build-commands": ["pip3 install --verbose --no-index --no-deps --no-compile --find-links=\"file://${PWD}\" "
                                 "--prefix=${FLATPAK_DEST} ./*.whl"],
              "sources": sources}
    with open(out, "w") as fh:
        json.dump(module, fh, indent=2)
        fh.write("\n")
    print(f"{len(sources)} wheels for {len(set(s['url'].rsplit('/', 1)[1].split('-')[0] for s in sources))} packages -> {out}")


if __name__ == "__main__":
    main()
