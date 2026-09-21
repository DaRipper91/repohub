import pytest

from repohub.core.auth import HostTokens, find_host_tokens
from repohub.core.hosts import BUILTIN_HOSTS, HostRegistry, HostSpec
from repohub.core.providers.forgejo import ForgejoProvider
from repohub.core.providers.github import GitHubProvider
from repohub.core.providers.gitlab import GitLabProvider
from repohub.config import make_providers


def _forge(host_id, domain, env):
    return HostSpec(host_id, "forgejo", host_id, domain, f"https://{domain}/api/v1", (env,))


def _setup():
    reg = HostRegistry(BUILTIN_HOSTS + (_forge("one", "one.example.org", "REPOHUB_ONE_TOKEN"),
                                        _forge("two", "two.example.org", "REPOHUB_TWO_TOKEN")))
    env = {"GITHUB_TOKEN": "tok-gh", "GITLAB_TOKEN": "tok-gl", "CODEBERG_TOKEN": "tok-cb",
           "REPOHUB_ONE_TOKEN": "tok-one", "REPOHUB_TWO_TOKEN": "tok-two"}
    return reg, find_host_tokens(reg, env, gh_cli=lambda: None)


def _auth_headers(provider):
    h = provider._client.headers
    return {k: h[k] for k in ("authorization", "private-token") if k in h}


def test_make_providers_classes_and_bases():
    reg, tokens = _setup()
    ps = make_providers(reg, tokens)
    assert list(ps) == list(reg.ids)
    assert isinstance(ps["github"], GitHubProvider)
    assert isinstance(ps["gitlab"], GitLabProvider)
    for hid in ("codeberg", "one", "two"):
        assert isinstance(ps[hid], ForgejoProvider)
    assert str(ps["one"]._client.base_url).rstrip("/") == "https://one.example.org/api/v1"
    assert str(ps["codeberg"]._client.base_url).rstrip("/") == "https://codeberg.org/api/v1"


def test_each_provider_gets_only_its_own_token():
    reg, tokens = _setup()
    ps = make_providers(reg, tokens)
    assert _auth_headers(ps["github"]) == {"authorization": "Bearer tok-gh"}
    assert _auth_headers(ps["gitlab"]) == {"private-token": "tok-gl"}
    assert _auth_headers(ps["codeberg"]) == {"authorization": "token tok-cb"}
    assert _auth_headers(ps["one"]) == {"authorization": "token tok-one"}
    assert _auth_headers(ps["two"]) == {"authorization": "token tok-two"}
    for hid, mine in {"github": "tok-gh", "gitlab": "tok-gl", "codeberg": "tok-cb",
                      "one": "tok-one", "two": "tok-two"}.items():
        blob = repr(dict(ps[hid]._client.headers)) + str(ps[hid]._client.base_url)
        for other in ("tok-gh", "tok-gl", "tok-cb", "tok-one", "tok-two"):
            assert (other in blob) == (other == mine)


def test_hosts_without_a_token_are_anonymous():
    reg, _ = _setup()
    ps = make_providers(reg, find_host_tokens(reg, {"REPOHUB_ONE_TOKEN": "tok-one"}, gh_cli=lambda: None))
    assert _auth_headers(ps["github"]) == {} and _auth_headers(ps["two"]) == {}
    assert _auth_headers(ps["one"]) == {"authorization": "token tok-one"}


def test_unregistered_token_env_reaches_no_provider():
    reg = HostRegistry(BUILTIN_HOSTS)
    tokens = find_host_tokens(reg, {"REPOHUB_ONE_TOKEN": "tok-stray", "AWS_SESSION_TOKEN": "tok-aws"},
                              gh_cli=lambda: None)
    for p in make_providers(reg, tokens).values():
        blob = repr(dict(p._client.headers))
        assert "tok-stray" not in blob and "tok-aws" not in blob


def test_shared_env_var_in_hand_built_registry_is_rejected():
    reg = HostRegistry(BUILTIN_HOSTS + (_forge("one", "one.example.org", "REPOHUB_X_TOKEN"),
                                        _forge("two", "two.example.org", "REPOHUB_X_TOKEN")))
    with pytest.raises(ValueError):
        find_host_tokens(reg, {"REPOHUB_X_TOKEN": "tok-x"}, gh_cli=lambda: None)
    with pytest.raises(ValueError):
        make_providers(reg, HostTokens({"one": "tok-x", "two": "tok-x"}))


def test_make_providers_rejects_unknown_kind():
    reg = HostRegistry(BUILTIN_HOSTS)
    import dataclasses
    # cannot happen via the constructor; replace() copies, the shared built-in spec stays untouched
    reg._specs["gitlab"] = dataclasses.replace(reg._specs["gitlab"], kind="svn")
    with pytest.raises(ValueError):
        make_providers(reg, HostTokens({}))


def test_make_providers_defaults_to_active_registry_and_empty_tokens():
    ps = make_providers(None, HostTokens({}))
    assert list(ps) == ["github", "gitlab", "codeberg"]


def test_hub_carries_host_problems(tmp_path):
    from repohub.core.cache import Cache
    from repohub.core.hub import Hub
    from repohub.core.store import Favorites

    db = str(tmp_path / "t.db")
    assert Hub({}, Cache(db), Favorites(db)).host_problems == []
    assert Hub({}, Cache(db), Favorites(db), host_problems=["p"]).host_problems == ["p"]


def test_build_hub_wires_hosts_tokens_and_problems_offline(tmp_path, monkeypatch):
    """The one test that calls build_hub(): tmp config/data dirs, no real gh, no network."""
    import respx

    from repohub.config import build_hub

    cfg, data, empty_bin = tmp_path / "cfg", tmp_path / "data", tmp_path / "bin"
    for d in (cfg / "repohub", data, empty_bin):
        d.mkdir(parents=True)
    (cfg / "repohub" / "hosts.yaml").write_text(
        "- {id: myforge, kind: forgejo, url: https://git.example.org, token_env: REPOHUB_MYFORGE_TOKEN}\n"
        "- {id: broken, kind: nope, url: 'http://x'}\n", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg))
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    monkeypatch.setenv("PATH", str(empty_bin))  # the real gh cannot run
    for var in ("GITHUB_TOKEN", "GH_TOKEN", "GITLAB_TOKEN", "CODEBERG_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("REPOHUB_MYFORGE_TOKEN", "fake-myforge-token")

    # the module-default gh_cli must find nothing when PATH is empty
    assert not find_host_tokens(HostRegistry(BUILTIN_HOSTS)).has("github")

    with respx.mock(assert_all_mocked=True):  # no routes: any request would fail the test
        hub = build_hub()

    ps = hub.providers
    assert list(ps) == ["github", "gitlab", "codeberg", "myforge"]
    assert isinstance(ps["github"], GitHubProvider) and isinstance(ps["gitlab"], GitLabProvider)
    assert isinstance(ps["codeberg"], ForgejoProvider) and isinstance(ps["myforge"], ForgejoProvider)
    assert ps["codeberg"].host == "codeberg"
    assert str(ps["codeberg"]._client.base_url).rstrip("/") == "https://codeberg.org/api/v1"
    assert ps["myforge"].host == "myforge"
    assert str(ps["myforge"]._client.base_url).rstrip("/") == "https://git.example.org/api/v1"
    assert _auth_headers(ps["github"]) == {} and _auth_headers(ps["gitlab"]) == {}
    assert _auth_headers(ps["codeberg"]) == {}
    assert _auth_headers(ps["myforge"]) == {"authorization": "token fake-myforge-token"}
    assert len(hub.host_problems) == 1 and "entry #1" in hub.host_problems[0]
    assert (data / "repohub" / "repohub.db").exists()
