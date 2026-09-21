"""Runs ONE approved build step for a cloned repository. Everything that can go wrong is checked first.

The step must come from the fixed plan (``runplan``), the folder must be a real clone found by the clone
scan, the plan must still match the digest the user approved, and only one command runs at a time. The
child gets a minimal environment (no tokens), no stdin, its own process group, a time limit and a capped
output buffer. No shell is ever involved.
"""
from __future__ import annotations

import os
import re
import secrets
import shutil
import signal
import subprocess
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from repohub.core.actionlog import ActionLog
from repohub.core.awareness import Awareness
from repohub.core.clonescan import _origin_url, _read_config, parse_remote
from repohub.core.hosts import registry
from repohub.core.runplan import VENV, Plan, build_plan
from repohub.core.settings import Settings
from repohub.core.textsafe import clean_text

TIMEOUT = 1800.0
MAX_OUTPUT = 200_000
KEEP_SESSIONS = 10
KILL_GRACE = 5.0
ENV_KEEP = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR", "USER", "LOGNAME")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")

RUNNING, DONE, FAILED, CANCELLED, TIMEOUT_STATUS = "running", "done", "failed", "cancelled", "timed out"


class RunError(Exception):
    """A refusal, with a short message that is safe to show."""


def clean_output(raw: bytes) -> str:
    """Plain text only: no ANSI escapes, no control or bidi characters; carriage returns become newlines."""
    text = raw.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")
    return clean_text(_ANSI.sub("", text), multiline=True)


@dataclass
class RunSession:
    id: str
    repo_key: str
    step_id: str
    command: str
    folder: str
    status: str = RUNNING
    exit_code: int | None = None
    started: float = 0.0
    finished: float | None = None
    truncated: bool = False
    _buf: bytearray = field(default_factory=bytearray, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _proc: object = field(default=None, repr=False)
    _cancel: bool = field(default=False, repr=False)

    @property
    def output(self) -> str:
        with self._lock:
            raw = bytes(self._buf)
        return clean_output(raw)

    @property
    def running(self) -> bool:
        return self.status == RUNNING

    def append(self, chunk: bytes, limit: int) -> None:
        with self._lock:
            self._buf += chunk
            if len(self._buf) > limit:
                del self._buf[: len(self._buf) - limit]  # keep the tail
                self.truncated = True


def _origin_matches(folder: Path, key: str) -> bool:
    text = _read_config(folder)
    url = _origin_url(text) if text else None
    parsed = parse_remote(url) if url else None
    return bool(parsed) and f"{parsed[0]}:{parsed[1].lower()}" == key


class Runner:
    def __init__(self, awareness: Awareness, settings: Settings, actions: ActionLog | None = None, *,
                 plan_fn: Callable[[Path], Plan] = build_plan, which: Callable = shutil.which,
                 popen: Callable = subprocess.Popen, environ=None, timeout: float = TIMEOUT,
                 max_output: int = MAX_OUTPUT):
        self.awareness, self.settings, self.actions = awareness, settings, actions
        self._plan_fn, self._which, self._popen = plan_fn, which, popen
        self._environ = environ if environ is not None else os.environ
        self.timeout, self.max_output = timeout, max_output
        self._sessions: OrderedDict[str, RunSession] = OrderedDict()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ reading

    @property
    def enabled(self) -> bool:
        return self.settings.get("guided_run")

    def find(self, host: str, slug: str):
        """(clone, plan) for a repository that is cloned, else (None, None)."""
        if not registry().slug_ok(host, slug):
            return None, None
        clone = self.awareness.cloned().get(f"{host}:{slug.lower()}")
        if clone is None:
            return None, None
        return clone, self._plan_fn(Path(clone.path))

    def get(self, session_id: str) -> RunSession | None:
        with self._lock:
            return self._sessions.get(session_id)

    def current(self) -> RunSession | None:
        with self._lock:
            return next((s for s in self._sessions.values() if s.running), None)

    # ------------------------------------------------------------------ starting

    def _child_env(self) -> dict[str, str]:
        return {k: self._environ[k] for k in ENV_KEEP if k in self._environ}

    def _resolve(self, folder: Path, name: str) -> str:
        if name.startswith(VENV):
            exe = folder / ".venv" / name[len(VENV) + 1:]
            if not exe.is_file() or not str(exe.resolve()).startswith(str((folder / ".venv").resolve()) + os.sep):
                raise RunError("the virtual environment step has not been run yet")
            return str(exe)
        found = self._which(name)
        if not found or not os.path.isabs(found):
            raise RunError(f"{clean_text(name)} was not found on this machine")
        real = os.path.realpath(found)
        if real == str(folder.resolve()) or real.startswith(str(folder.resolve()) + os.sep):
            raise RunError(f"refusing to run {clean_text(name)} from inside the repository")
        return found

    def start(self, host: str, slug: str, step_id: str, digest: str) -> RunSession:
        if not self.enabled:
            raise RunError("guided install is turned off")
        if not registry().slug_ok(host, slug):
            raise RunError("invalid repository")
        key = f"{host}:{slug.lower()}"
        clone = self.awareness.cloned(refresh=True).get(key)
        if clone is None:
            raise RunError("this repository is not cloned in a known folder")
        folder = Path(clone.path)
        try:
            if folder.is_symlink() or not folder.is_dir():
                raise RunError("the clone folder is not a plain folder")
        except OSError:
            raise RunError("the clone folder cannot be read") from None
        if not _origin_matches(folder, key):
            raise RunError("the folder's origin no longer matches this repository")
        plan = self._plan_fn(folder)
        if plan.digest != digest:
            raise RunError("the proposed commands changed; reload the page and review them again")
        step = plan.step(step_id)
        if step is None:
            raise RunError("no such step")
        exe = self._resolve(folder, step.argv[0])
        argv = [exe, *step.argv[1:]]
        with self._lock:
            if any(s.running for s in self._sessions.values()):
                raise RunError("another command is still running")
            session = RunSession(secrets.token_urlsafe(9), key, step.id, step.text(), str(folder),
                                 started=time.monotonic())
            try:
                proc = self._popen(argv, cwd=str(folder), env=self._child_env(), stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, shell=False,
                                   start_new_session=True, close_fds=True)
            except OSError:
                raise RunError("the command could not be started") from None
            session._proc = proc
            self._sessions[session.id] = session
            while len(self._sessions) > KEEP_SESSIONS:
                oldest = next(iter(self._sessions))
                if self._sessions[oldest].running:
                    break
                del self._sessions[oldest]
        threading.Thread(target=self._pump, args=(session, host, slug), name="repohub-run", daemon=True).start()
        return session

    # ------------------------------------------------------------------ running

    def _kill(self, session: RunSession, hard: bool = False) -> None:
        proc = session._proc
        try:
            if proc.poll() is not None:  # the leader is gone: its group id may already belong to someone else
                return
            os.killpg(proc.pid, signal.SIGKILL if hard else signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    def _pump(self, session: RunSession, host: str, slug: str) -> None:
        proc = session._proc
        timed_out = threading.Event()

        def on_timeout() -> None:
            timed_out.set()
            self._kill(session)
            threading.Timer(KILL_GRACE, self._kill, args=(session, True)).start()

        timer = threading.Timer(self.timeout, on_timeout)
        timer.daemon = True
        timer.start()
        try:
            while True:
                chunk = proc.stdout.read1(4096) if hasattr(proc.stdout, "read1") else proc.stdout.read(4096)
                if not chunk:
                    break
                session.append(chunk, self.max_output)
            code = proc.wait()
        except Exception:
            self._kill(session, hard=True)
            code = -1
        finally:
            timer.cancel()
        session.exit_code = code
        session.finished = time.monotonic()
        if session._cancel:
            session.status = CANCELLED
        elif timed_out.is_set():
            session.status = TIMEOUT_STATUS
        else:
            session.status = DONE if code == 0 else FAILED
        if self.actions is not None:
            try:
                self.actions.add(host, slug, "run", session.status == DONE, f"{session.step_id}: {session.status} (exit {code})")
            except Exception:
                pass

    def cancel(self, session_id: str) -> bool:
        session = self.get(session_id)
        if session is None or not session.running:
            return False
        session._cancel = True
        self._kill(session)
        threading.Timer(KILL_GRACE, self._kill, args=(session, True)).start()
        return True
