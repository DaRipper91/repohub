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
