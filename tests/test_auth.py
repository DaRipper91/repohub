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
