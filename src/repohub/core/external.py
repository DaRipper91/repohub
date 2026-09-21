"""Helpers for handing a repository to another program the user has installed (ghgrab, Claude Code).

RepoHub never downloads or runs anything here: it builds the exact command for the user to copy, or, in the
terminal app and after a y/n, starts the user's own program. The program is looked up on PATH and refused when
the result is not an absolute path or lives inside a folder it must not come from (for example the repository).
"""
from __future__ import annotations

import os
import shlex
import shutil
from typing import Callable

from repohub.core.hosts import registry

GHGRAB_INSTALL = "pipx install ghgrab   (or: cargo install ghgrab, npm install -g @ghgrab/ghgrab)"


def find_tool(name: str, avoid: str | None = None, which: Callable[[str], str | None] | None = None) -> str | None:
    """Absolute path of ``name`` on PATH, or None. Refused if relative or inside the ``avoid`` folder."""
    found = (which or shutil.which)(name)  # looked up at call time
    if not found or not os.path.isabs(found):
        return None
    if avoid:
        real, root = os.path.realpath(found), os.path.realpath(avoid)
        if real == root or real.startswith(root + os.sep):
            return None
    return found


def repo_url(host: str, slug: str) -> str | None:
    """The web address of a repository on a configured host, or None for anything that is not one."""
    spec = registry().get(host)
    if spec is None or len(slug) > 200 or not registry().slug_ok(host, slug):
        return None
    return f"{spec.web_base}/{slug}"


def grab_command(host: str, slug: str) -> str | None:
    """``ghgrab <url>``: browse the repository and download single files or folders, without cloning."""
    url = repo_url(host, slug)
    return f"ghgrab {shlex.quote(url)}" if url else None


def release_command(host: str, slug: str) -> str | None:
    """``ghgrab rel owner/name``: download the release build matching this OS and CPU (GitHub only)."""
    if host != "github" or repo_url(host, slug) is None:
        return None
    return f"ghgrab rel {shlex.quote(slug)}"
