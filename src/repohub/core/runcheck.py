"""'Can I run this here?': a checklist plus a one-line verdict. Advisory only; nothing is run."""
from __future__ import annotations

from dataclasses import dataclass, field

from repohub.core.clonescan import CloneInfo
from repohub.core.machine import LANGUAGE_KINDS, TOOLS, Machine
from repohub.core.models import Release, Repo
from repohub.core.textsafe import clean_text

OK, WARN, NO, INFO = "ok", "warn", "no", "info"
LIKELY, MAYBE, SETUP, UNLIKELY, UNKNOWN = "likely", "maybe", "needs setup", "unlikely", "unknown"


@dataclass(frozen=True)
class Check:
    label: str
    status: str
    detail: str


@dataclass(frozen=True)
class Verdict:
    level: str
    summary: str
    checks: tuple[Check, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {"verdict": self.level, "summary": self.summary,
                "checks": [{"label": c.label, "status": c.status, "detail": c.detail} for c in self.checks]}


def kinds_for(repo: Repo, detected: list[str]) -> list[str]:
    """Project kinds: from the cloned folder when there is one, else guessed from the language."""
    if detected:
        return list(detected)
    guess = LANGUAGE_KINDS.get(clean_text(repo.language).lower())
    return [guess] if guess else []


def run_check(repo: Repo, release: Release | None, machine: Machine, clone: CloneInfo | None = None,
              detected: list[str] | None = None) -> Verdict:
    checks: list[Check] = []
    prebuilt = None  # True: a build for this CPU exists; False: builds exist but not for it; None: no release

    if release and release.assets:
        arches = {a.arch for a in release.assets}
        if machine.arch in arches:
            prebuilt = True
            checks.append(Check("Ready-made build", OK, f"the latest release ({clean_text(release.tag)}) has a {machine.arch} build"))
        elif arches <= {"unknown"}:
            checks.append(Check("Ready-made build", WARN, "the release files do not say which CPU they are for"))
        else:
            prebuilt = False
            have = ", ".join(sorted(a for a in arches if a != "unknown")) or "other"
            checks.append(Check("Ready-made build", NO, f"the release ships {have} builds, not {machine.arch}"))
    else:
        checks.append(Check("Ready-made build", INFO, "no release published: it has to be built from source"))

    kinds = kinds_for(repo, detected or [])
    missing: list[str] = []
    if kinds:
        for kind in kinds:
            need = TOOLS.get(kind)
            if not need:
                continue
            lacking = [" or ".join(alts) for alts in need if not machine.has(alts)]
            if lacking:
                missing += lacking
                checks.append(Check(f"Tools for {kind}", NO, "missing: " + ", ".join(lacking)))
            else:
                checks.append(Check(f"Tools for {kind}", OK, "found on this machine"))
        source = "found in the cloned folder" if detected else "guessed from the language"
        checks.append(Check("Project type", INFO, f"{', '.join(kinds)} ({source})"))
    else:
        checks.append(Check("Project type", INFO, "could not tell what it needs to build"))

    checks.append(Check("Already cloned", OK if clone else INFO, f"at {clean_text(clone.path)}" if clone else "not cloned yet"))
    ram = f"{machine.ram_gb} GB memory" if machine.ram_gb else "memory unknown"
    checks.append(Check("This machine", INFO, f"{machine.arch}, {clean_text(machine.system)}, {ram}"))

    if prebuilt:
        level, summary = LIKELY, f"A ready-made {machine.arch} build exists."
    elif kinds and not missing and prebuilt is not False:
        level, summary = MAYBE, "You have the tools to build it from source. RepoHub does not check its libraries."
    elif kinds and not missing:
        level, summary = MAYBE, "No build for this CPU, but you have the tools to build it from source."
    elif missing:
        level, summary = SETUP, "Missing tools: " + ", ".join(missing) + "."
    elif prebuilt is False:
        level, summary = UNLIKELY, f"Its releases are not built for {machine.arch}."
    else:
        level, summary = UNKNOWN, "Not enough information to say."
    return Verdict(level, summary, tuple(checks))
