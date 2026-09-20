from pathlib import Path
import subprocess

import pytest

from repohub.core.clone import CloneError, clone, clone_url, plan_clone


def test_clone_url_builds_from_host_and_slug():
    assert clone_url("github", "o/r") == "https://github.com/o/r.git"
    assert clone_url("gitlab", "g/sub/p") == "https://gitlab.com/g/sub/p.git"


@pytest.mark.parametrize("host,slug", [("bitbucket", "o/r"), ("github", "../x"), ("github", "o")])
def test_clone_url_rejects_bad_input(host, slug):
    with pytest.raises(CloneError):
        clone_url(host, slug)


@pytest.mark.parametrize("url", [
    "http://github.com/a/b", "https://evil.com/a/b", "https://github.com.evil.com/a/b",
    "https://user@github.com/a/b", "https://github.com:444/a/b", "https://github.com/a",
    "https://github.com/a/../b", "git@github.com:a/b", "https://github.com/a/-rf", "file:///etc/passwd",
])
def test_plan_rejects_unsafe_urls(url, tmp_path):
    with pytest.raises(CloneError):
        plan_clone(url, tmp_path)


def test_plan_returns_target_inside_root(tmp_path):
    assert plan_clone("https://github.com/a/b.git", tmp_path) == tmp_path.resolve() / "b"


def test_plan_refuses_existing_folder(tmp_path):
    (tmp_path / "b").mkdir()
    with pytest.raises(CloneError, match="exists"):
        plan_clone("https://github.com/a/b.git", tmp_path)


def test_clone_runs_git_without_shell_and_shallow_by_default(tmp_path):
    seen = {}

    def runner(args, **kw):
        seen["args"], seen["kw"] = args, kw
        return subprocess.CompletedProcess(args, 0, "", "")

    target = clone("https://github.com/a/b.git", tmp_path, runner=runner)
    assert target == tmp_path.resolve() / "b"
    assert seen["args"] == ["git", "clone", "--depth", "1", "--", "https://github.com/a/b.git", str(target)]
    assert not seen["kw"].get("shell")
    assert seen["kw"]["env"]["GIT_TERMINAL_PROMPT"] == "0"


def test_clone_failure_becomes_clone_error(tmp_path):
    def runner(args, **kw):
        return subprocess.CompletedProcess(args, 128, "", "fatal: repository not found")

    with pytest.raises(CloneError, match="not found"):
        clone("https://github.com/a/b.git", tmp_path, runner=runner)


@pytest.mark.parametrize("url", [
    "https://github.com/a/b\n", "https://github.com/a/b\tx", " https://github.com/a/b",
    "https://github.com/a/b?x=1", "https://github.com/a/b#f", "https://github.com:/a/b",
    "https://github.com/a/b;x", None, "",
])
def test_plan_rejects_ambiguous_or_odd_input(url, tmp_path):
    with pytest.raises(CloneError):
        plan_clone(url, tmp_path)


def _ok_runner(seen):
    def runner(args, **kw):
        seen["args"], seen["kw"] = args, kw
        return subprocess.CompletedProcess(args, 0, "", "")
    return runner


def test_clone_passes_canonical_url_to_git(tmp_path):
    seen = {}
    target = clone("HTTPS://GitHub.com/a/b", tmp_path, runner=_ok_runner(seen))
    assert seen["args"][-2] == "https://github.com/a/b.git"
    assert seen["args"][-1] == str(target)


def test_clone_timeout_cleans_up(tmp_path):
    def runner(args, **kw):
        (tmp_path / "b").mkdir()
        raise subprocess.TimeoutExpired(args, 600)

    with pytest.raises(CloneError, match="timed out"):
        clone("https://github.com/a/b.git", tmp_path, runner=runner)
    assert not (tmp_path / "b").exists()


def test_clone_missing_git(tmp_path):
    def runner(args, **kw):
        raise FileNotFoundError("git")

    with pytest.raises(CloneError, match="could not be run"):
        clone("https://github.com/a/b.git", tmp_path, runner=runner)


def test_clone_blank_stderr_gives_generic_message(tmp_path):
    def runner(args, **kw):
        return subprocess.CompletedProcess(args, 128, "", "   \n")

    with pytest.raises(CloneError, match="git clone failed"):
        clone("https://github.com/a/b.git", tmp_path, runner=runner)


def test_clone_error_redacts_credentials(tmp_path):
    def runner(args, **kw):
        return subprocess.CompletedProcess(
            args, 128, "", "fatal: unable to access 'https://tok@github.com/a/q/'\n\n")

    with pytest.raises(CloneError) as ei:
        clone("https://github.com/a/b.git", tmp_path, runner=runner)
    assert "***" in str(ei.value) and "tok@" not in str(ei.value)


def test_failed_clone_removes_partial_dir_only(tmp_path):
    sibling = tmp_path / "keep"
    sibling.mkdir()

    def runner(args, **kw):
        (tmp_path / "b").mkdir()
        (tmp_path / "b" / "partial").write_text("x")
        return subprocess.CompletedProcess(args, 128, "", "fatal: boom")

    with pytest.raises(CloneError):
        clone("https://github.com/a/b.git", tmp_path, runner=runner)
    assert not (tmp_path / "b").exists()
    assert sibling.is_dir()


def test_clone_locks_down_git_environment(tmp_path):
    import os
    seen = {}
    clone("https://github.com/a/b.git", tmp_path, runner=_ok_runner(seen))
    env = seen["kw"]["env"]
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_ALLOW_PROTOCOL"] == "https"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull


def test_clone_root_that_is_a_file_is_clone_error(tmp_path):
    f = tmp_path / "afile"
    f.write_text("x")
    ran = []
    with pytest.raises(CloneError, match="cannot create destination folder"):
        clone("https://github.com/o/r.git", f, runner=lambda *a, **k: ran.append(1))
    assert ran == [] and f.read_text() == "x"


# --- concurrent clones of the same target ---
import threading


def _blocking_runner(started, release, rc=0):
    def runner(args, **kwargs):
        started.set()
        assert release.wait(5)
        if rc == 0:
            target = Path(args[-1])
            target.mkdir(parents=True)
            (target / "file.txt").write_text("done")
        return subprocess.CompletedProcess(args, rc, stdout="", stderr="fatal: boom" if rc else "")

    return runner


def test_concurrent_clone_of_same_target_is_refused_and_keeps_winner(tmp_path):
    started, release = threading.Event(), threading.Event()
    result = {}

    def first():
        result["path"] = clone("https://github.com/a/b", tmp_path, runner=_blocking_runner(started, release))

    t = threading.Thread(target=first)
    t.start()
    assert started.wait(5)
    with pytest.raises(CloneError, match="in progress"):
        clone("https://github.com/a/b", tmp_path, runner=lambda *a, **k: pytest.fail("must not run git"))
    release.set()
    t.join(5)
    assert result["path"] == tmp_path.resolve() / "b"
    assert (tmp_path / "b" / "file.txt").read_text() == "done"


def test_claim_released_after_success_and_failure(tmp_path):
    ok = threading.Event()
    ok.set()
    failing = _blocking_runner(threading.Event(), ok, rc=1)
    with pytest.raises(CloneError, match="boom"):
        clone("https://github.com/a/b", tmp_path, runner=failing)
    assert clone("https://github.com/a/b", tmp_path, runner=_blocking_runner(threading.Event(), ok)).name == "b"
    with pytest.raises(CloneError, match="exists"):
        clone("https://github.com/a/b", tmp_path, runner=failing)
    (tmp_path / "b").rename(tmp_path / "moved")
    assert clone("https://github.com/a/b", tmp_path, runner=_blocking_runner(threading.Event(), ok)).name == "b"


def test_different_targets_clone_concurrently(tmp_path):
    started, release = threading.Event(), threading.Event()
    t = threading.Thread(target=lambda: clone("https://github.com/a/one", tmp_path, runner=_blocking_runner(started, release)))
    t.start()
    assert started.wait(5)
    other = threading.Event()
    other.set()
    assert clone("https://github.com/a/two", tmp_path, runner=_blocking_runner(threading.Event(), other)).name == "two"
    release.set()
    t.join(5)
    assert (tmp_path / "one").is_dir()
