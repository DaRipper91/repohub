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
