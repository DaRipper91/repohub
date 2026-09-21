from repohub.core.auth import Tokens, find_tokens


def no_cli():
    return None


def test_env_tokens_win_over_gh_cli():
    t = find_tokens({"GITHUB_TOKEN": "envgh", "GITLAB_TOKEN": "envgl"}, gh_cli=lambda: "clitoken")
    assert (t.github, t.gitlab) == ("envgh", "envgl")


def test_gh_token_alias_is_accepted():
    assert find_tokens({"GH_TOKEN": "x"}, gh_cli=no_cli).github == "x"


def test_falls_back_to_gh_cli():
    assert find_tokens({}, gh_cli=lambda: "clitoken").github == "clitoken"


def test_anonymous_when_nothing_found():
    t = find_tokens({}, gh_cli=no_cli)
    assert t.github is None and t.gitlab is None


def test_empty_values_count_as_missing():
    assert find_tokens({"GITHUB_TOKEN": ""}, gh_cli=no_cli).github is None


def test_repr_never_contains_tokens():
    t = Tokens(github="secret-gh", gitlab="secret-gl")
    assert "secret" not in repr(t) and "secret" not in str(t)


# --- whitespace handling and the gh CLI helper ---
import subprocess

import pytest

from repohub.core import auth


def _fake_run(monkeypatch, result=None, exc=None):
    def run(*args, **kwargs):
        if exc is not None:
            raise exc
        return result

    monkeypatch.setattr(auth.subprocess, "run", run)


def _completed(stdout="", returncode=0):
    return subprocess.CompletedProcess(["gh", "auth", "token"], returncode, stdout=stdout, stderr="")


def test_env_token_is_stripped():
    assert find_tokens({"GITHUB_TOKEN": " abc\n"}, gh_cli=no_cli).github == "abc"


def test_gitlab_token_is_stripped():
    assert find_tokens({"GITLAB_TOKEN": "\tgl \n"}, gh_cli=no_cli).gitlab == "gl"


def test_whitespace_only_env_falls_back_to_gh_cli():
    assert find_tokens({"GITHUB_TOKEN": "  "}, gh_cli=lambda: "clitoken").github == "clitoken"


def test_whitespace_only_gitlab_is_missing():
    assert find_tokens({"GITLAB_TOKEN": " \n"}, gh_cli=no_cli).gitlab is None


def test_gh_cli_value_is_stripped_in_find_tokens():
    assert find_tokens({}, gh_cli=lambda: " tok\n").github == "tok"


def test_gh_cli_whitespace_only_is_missing():
    assert find_tokens({}, gh_cli=lambda: "  \n").github is None


@pytest.mark.parametrize(
    "exc",
    [
        FileNotFoundError("gh"),
        subprocess.TimeoutExpired("gh", 5),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad"),
    ],
)
def test_gh_cli_errors_return_none(monkeypatch, exc):
    _fake_run(monkeypatch, exc=exc)
    assert auth._gh_cli_token() is None


def test_gh_cli_nonzero_exit_returns_none(monkeypatch):
    _fake_run(monkeypatch, _completed("tok\n", returncode=1))
    assert auth._gh_cli_token() is None


def test_gh_cli_success_returns_stripped_token_and_prints_nothing(monkeypatch, capsys):
    _fake_run(monkeypatch, _completed("  ghp_secret\n"))
    assert auth._gh_cli_token() == "ghp_secret"
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


# --- per-host tokens from the registry ---
import os

from repohub.core.auth import HostTokens, find_host_tokens
from repohub.core.hosts import BUILTIN_HOSTS, HostRegistry, HostSpec


def _forge(host_id, domain, *env):
    return HostSpec(host_id, "forgejo", host_id, domain, f"https://{domain}/api/v1", tuple(env))


def _reg(*extra):
    return HostRegistry(BUILTIN_HOSTS + tuple(extra))


def boom_cli():
    raise AssertionError("gh_cli must not be called")


def gh_only(value=None):
    calls = []

    def cli():
        calls.append(1)
        return value

    cli.calls = calls
    return cli


def test_host_tokens_per_host_from_env():
    reg = _reg(_forge("myforge", "git.example.org", "REPOHUB_MYFORGE_TOKEN"))
    env = {"CODEBERG_TOKEN": "cb", "REPOHUB_MYFORGE_TOKEN": "mf", "GITLAB_TOKEN": "gl", "GH_TOKEN": "gh"}
    t = find_host_tokens(reg, env, gh_cli=boom_cli)
    assert (t.for_host("codeberg"), t.for_host("myforge"), t.for_host("gitlab"), t.for_host("github")) == ("cb", "mf", "gl", "gh")


def test_first_variable_in_order_wins():
    env = {"GITHUB_TOKEN": "first", "GH_TOKEN": "second"}
    assert find_host_tokens(_reg(), env, gh_cli=boom_cli).for_host("github") == "first"
    env = {"GITHUB_TOKEN": "  ", "GH_TOKEN": "second"}
    assert find_host_tokens(_reg(), env, gh_cli=boom_cli).for_host("github") == "second"


def test_values_stripped_and_empty_ignored():
    env = {"CODEBERG_TOKEN": " \tcb\n", "GITLAB_TOKEN": "", "GITHUB_TOKEN": "   \n"}
    t = find_host_tokens(_reg(), env, gh_cli=no_cli)
    assert t.for_host("codeberg") == "cb"
    assert t.for_host("gitlab") is None and not t.has("gitlab")
    assert t.for_host("github") is None
    assert t.has("codeberg")


def test_gh_cli_only_for_github_and_not_when_env_present():
    cli = gh_only("clitok")
    t = find_host_tokens(_reg(), {}, gh_cli=cli)
    assert t.for_host("github") == "clitok" and len(cli.calls) == 1
    assert t.for_host("gitlab") is None and t.for_host("codeberg") is None
    cli = gh_only("clitok")
    find_host_tokens(_reg(), {"GH_TOKEN": "x"}, gh_cli=cli)
    assert cli.calls == []


def test_gh_cli_value_stripped_and_blank_ignored():
    assert find_host_tokens(_reg(), {}, gh_cli=lambda: " tok\n").for_host("github") == "tok"
    assert find_host_tokens(_reg(), {}, gh_cli=lambda: "  ").for_host("github") is None


def test_unknown_host_has_no_token():
    t = find_host_tokens(_reg(), {"GITLAB_TOKEN": "gl"}, gh_cli=no_cli)
    assert t.for_host("nope") is None and not t.has("nope")


def test_unregistered_token_variable_is_never_read():
    reg = _reg()
    env = {"REPOHUB_GHOST_TOKEN": "ghost-secret", "AWS_SESSION_TOKEN": "aws-secret", "GITHUB_TOKEN": "gh"}
    t = find_host_tokens(reg, env, gh_cli=boom_cli)
    assert t.for_host("ghost") is None
    blob = repr(t) + str(t) + repr(vars(t))
    assert "ghost-secret" not in blob and "aws-secret" not in blob
    class Spy(dict):
        def __init__(self, d):
            super().__init__(d)
            self.read = []
        def get(self, key, default=None):
            self.read.append(key)
            return super().get(key, default)
        def __getitem__(self, key):
            self.read.append(key)
            return super().__getitem__(key)
    spy = Spy(env)
    find_host_tokens(reg, spy, gh_cli=boom_cli)
    assert set(spy.read) <= {"GITHUB_TOKEN", "GH_TOKEN", "GITLAB_TOKEN", "CODEBERG_TOKEN"}


def test_env_names_are_case_sensitive():
    t = find_host_tokens(_reg(), {"codeberg_token": "x", "Gitlab_Token": "y"}, gh_cli=no_cli)
    assert t.for_host("codeberg") is None and t.for_host("gitlab") is None


def test_explicit_env_never_reads_os_environ(monkeypatch):
    monkeypatch.setenv("CODEBERG_TOKEN", "real-env-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "real-env-secret")
    t = find_host_tokens(_reg(), {}, gh_cli=no_cli)
    assert t.for_host("codeberg") is None and t.for_host("github") is None


def test_default_env_is_os_environ(monkeypatch):
    monkeypatch.setenv("CODEBERG_TOKEN", "from-os")
    assert find_host_tokens(_reg(), None, gh_cli=no_cli).for_host("codeberg") == "from-os"


def test_default_registry_is_the_active_one():
    t = find_host_tokens(env={"CODEBERG_TOKEN": "cb"}, gh_cli=no_cli)
    assert t.for_host("codeberg") == "cb"


def test_host_tokens_never_show_values():
    env = {"CODEBERG_TOKEN": "sekrit-cb", "GITLAB_TOKEN": "sekrit-gl", "GH_TOKEN": "sekrit-gh"}
    t = find_host_tokens(_reg(), env, gh_cli=no_cli)
    for text in (repr(t), str(t), repr(vars(t)), str(vars(t)), f"{t!r}{t}", repr([t]), repr({"k": t})):
        assert "sekrit" not in text
    import dataclasses
    assert "sekrit" not in repr(dataclasses.replace(t))
    with pytest.raises(dataclasses.FrozenInstanceError):
        t.anything = 1


def test_host_tokens_direct_construction_and_errors_hide_values():
    t = HostTokens({"a": "sekrit-a"})
    assert t.for_host("a") == "sekrit-a" and "sekrit" not in repr(t)
    with pytest.raises(TypeError) as ei:
        t.for_host(["sekrit-a"])  # unhashable argument
    assert "sekrit" not in str(ei.value)


def test_shared_env_var_between_hosts_is_rejected():
    reg = _reg(_forge("one", "one.example.org", "REPOHUB_SHARED_TOKEN"),
               _forge("two", "two.example.org", "REPOHUB_SHARED_TOKEN"))
    with pytest.raises(ValueError) as ei:
        find_host_tokens(reg, {"REPOHUB_SHARED_TOKEN": "sekrit-shared"}, gh_cli=no_cli)
    assert "sekrit" not in str(ei.value) and "REPOHUB_SHARED_TOKEN" in str(ei.value)


def test_extra_host_reusing_builtin_env_var_is_rejected():
    reg = _reg(_forge("one", "one.example.org", "CODEBERG_TOKEN"))
    with pytest.raises(ValueError):
        find_host_tokens(reg, {"CODEBERG_TOKEN": "x"}, gh_cli=no_cli)
