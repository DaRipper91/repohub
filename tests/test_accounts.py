import io
import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

import repohub.cli as cli
from helpers import FakeProvider, make_hub
from repohub.core import accounts as acc
from repohub.core.accounts import ProviderAccount, RateLimit, star_fork_hint
from repohub.core.auth import find_host_tokens_and_sources, find_token_sources
from repohub.core.browse import Shelf
from repohub.core.cache import Cache
from repohub.core.hosts import BUILTIN_HOSTS, HostRegistry, HostSpec
from repohub.core.hub import Hub
from repohub.core.providers.base import ProviderError, RateLimited
from repohub.core.providers.forgejo import ForgejoProvider
from repohub.core.providers.github import GitHubProvider
from repohub.core.providers.gitlab import GitLabProvider
from repohub.core.store import Favorites
from repohub.web.app import create_app

SECRET = "ghp_SUPERSECRETVALUE123456"


@pytest.fixture(autouse=True)
def config_dir(monkeypatch, tmp_path):
    cfg = tmp_path / "xdg-config"
    cfg.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg))


# ---------------------------------------------------------------- token sources

def test_sources_name_variable_or_cli_never_value():
    reg = HostRegistry(BUILTIN_HOSTS)
    env = {"GH_TOKEN": SECRET, "GITLAB_TOKEN": "glpat-x"}
    src = find_token_sources(reg, env, gh_cli=lambda: pytest.fail("cli called"))
    assert src == {"github": "env GH_TOKEN", "gitlab": "env GITLAB_TOKEN"}
    assert SECRET not in json.dumps(src)


def test_sources_gh_cli_fallback_and_single_call():
    calls = []

    def cli_():
        calls.append(1)
        return SECRET

    tokens, src = find_host_tokens_and_sources(HostRegistry(BUILTIN_HOSTS), {}, gh_cli=cli_)
    assert src == {"github": "gh CLI"} and tokens.for_host("github") == SECRET and len(calls) == 1


def test_sources_extra_host():
    reg = HostRegistry(BUILTIN_HOSTS + (HostSpec("myforge", "forgejo", "M", "git.example.org",
                                                 "https://git.example.org/api/v1", ("REPOHUB_MYFORGE_TOKEN",)),))
    src = find_token_sources(reg, {"REPOHUB_MYFORGE_TOKEN": "t"}, gh_cli=lambda: None)
    assert src == {"myforge": "env REPOHUB_MYFORGE_TOKEN"}


# ---------------------------------------------------------------- pure helpers

def test_clean_scopes_and_login_reject_hostile_values():
    assert acc.clean_scopes(["repo", "read:org", "evil\x1b[31m", "<b>", "repo", 5, "a" * 50]) == ("repo", "read:org")
    assert len(acc.clean_scopes([f"s{i}" for i in range(100)])) == acc.MAX_SCOPES
    assert acc.clean_login("octo-cat") == "octo-cat"
    assert acc.clean_login("<script>") == "" and acc.clean_login(None) == ""


def test_parse_rate():
    assert acc.parse_rate({"x-limit": "5000", "x-remaining": "4990", "x-reset": "17"}, "x") == RateLimit(5000, 4990, 17)
    assert acc.parse_rate({"x-limit": "5000"}, "x") is None
    assert acc.parse_rate({"x-limit": "abc", "x-remaining": "1"}, "x") is None


@pytest.mark.parametrize("kind,scopes,answer", [
    ("github", None, "unknown"), ("github", ("repo",), "yes"), ("github", ("public_repo",), "yes"),
    ("github", ("read:org",), "no"), ("github", (), "no"),
    ("gitlab", ("api",), "yes"), ("gitlab", ("read_api",), "no"), ("gitlab", None, "unknown"),
    ("forgejo", None, "unknown"), ("forgejo", ("x",), "unknown")])
def test_star_fork_hint(kind, scopes, answer):
    assert star_fork_hint(kind, scopes)[0] == answer


def test_hint_has_fix_when_no():
    assert "gh auth refresh -s public_repo" in star_fork_hint("github", ("read:org",))[1]


# ---------------------------------------------------------------- providers

@respx.mock
async def test_github_account_classic_token():
    route = respx.get("https://api.github.com/user").mock(return_value=httpx.Response(
        200, json={"login": "octocat"},
        headers={"x-oauth-scopes": "repo, read:org", "x-ratelimit-limit": "5000", "x-ratelimit-remaining": "4999",
                 "x-ratelimit-reset": "1790000000"}))
    got = await GitHubProvider("tok").account()
    assert got == ProviderAccount("octocat", ("repo", "read:org"), RateLimit(5000, 4999, 1790000000))
    assert route.calls.last.request.headers["authorization"] == "Bearer tok"


@respx.mock
async def test_github_account_fine_grained_has_unknown_scopes():
    respx.get("https://api.github.com/user").mock(return_value=httpx.Response(200, json={"login": "o"}))
    assert (await GitHubProvider("tok").account()).scopes is None


@respx.mock
async def test_github_account_without_token_makes_no_request():
    route = respx.get("https://api.github.com/user").mock(return_value=httpx.Response(200, json={"login": "o"}))
    assert await GitHubProvider().account() is None and route.call_count == 0


@respx.mock
async def test_github_account_rejected_token():
    respx.get("https://api.github.com/user").mock(return_value=httpx.Response(401, json={}))
    p = GitHubProvider("bad")
    with pytest.raises(ProviderError, match="token rejected"):
        await p.account()
    assert p.token_rejected


@respx.mock
async def test_github_account_rate_limited():
    respx.get("https://api.github.com/user").mock(return_value=httpx.Response(
        403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1790000000"}, json={}))
    with pytest.raises(RateLimited):
        await GitHubProvider("t").account()


@respx.mock
async def test_github_account_malformed_body():
    respx.get("https://api.github.com/user").mock(return_value=httpx.Response(200, json={"nope": 1}))
    with pytest.raises(ProviderError, match="unexpected response"):
        await GitHubProvider("t").account()


@respx.mock
async def test_gitlab_account_with_scopes():
    respx.get("https://gitlab.com/api/v4/user").mock(return_value=httpx.Response(
        200, json={"username": "tanuki"}, headers={"ratelimit-limit": "2000", "ratelimit-remaining": "1990"}))
    respx.get("https://gitlab.com/api/v4/personal_access_tokens/self").mock(
        return_value=httpx.Response(200, json={"scopes": ["read_api", "api"]}))
    got = await GitLabProvider("tok").account()
    assert got == ProviderAccount("tanuki", ("read_api", "api"), RateLimit(2000, 1990, None))


@respx.mock
async def test_gitlab_account_scopes_unknown_when_self_endpoint_missing():
    respx.get("https://gitlab.com/api/v4/user").mock(return_value=httpx.Response(200, json={"username": "t"}))
    respx.get("https://gitlab.com/api/v4/personal_access_tokens/self").mock(return_value=httpx.Response(404))
    got = await GitLabProvider("tok").account()
    assert got.login == "t" and got.scopes is None


@respx.mock
async def test_gitlab_account_no_token():
    assert await GitLabProvider().account() is None


@respx.mock
async def test_forgejo_account():
    route = respx.get("https://codeberg.org/api/v1/user").mock(return_value=httpx.Response(200, json={"login": "cb"}))
    got = await ForgejoProvider("codeberg", "https://codeberg.org/api/v1", "tok").account()
    assert got == ProviderAccount("cb") and route.calls.last.request.headers["authorization"] == "token tok"


@respx.mock
async def test_forgejo_account_rejected_and_anonymous():
    respx.get("https://codeberg.org/api/v1/user").mock(return_value=httpx.Response(401))
    with pytest.raises(ProviderError, match="token rejected"):
        await ForgejoProvider("codeberg", "https://codeberg.org/api/v1", "bad").account()
    assert await ForgejoProvider("codeberg", "https://codeberg.org/api/v1").account() is None


# ---------------------------------------------------------------- hub

class AccountProvider(FakeProvider):
    def __init__(self, host, result=None, error=None, rejected=False):
        super().__init__(host)
        self.result, self.err, self.token_rejected, self.account_calls = result, error, rejected, 0

    async def account(self):
        self.account_calls += 1
        if self.err:
            raise self.err
        return self.result


def hub_of(*providers, sources=None, clock=None):
    kw = {"clock": clock} if clock else {}
    ckw = {"now": clock} if clock else {}
    return Hub({p.host: p for p in providers}, Cache(**ckw), Favorites(**ckw), token_sources=sources, **kw)


async def test_hub_signed_in_includes_source_and_hint():
    p = AccountProvider("github", ProviderAccount("me", ("read:org",), RateLimit(60, 50)))
    info = await hub_of(p, sources={"github": "gh CLI"}).account("github")
    assert (info.status, info.login, info.source, info.can_star_fork) == ("signed in", "me", "gh CLI", "no")
    assert "public_repo" in info.hint and info.rate.remaining == 50


async def test_hub_anonymous_when_no_token():
    info = await hub_of(AccountProvider("github", None)).account("github")
    assert info.status == "not signed in" and info.source == "" and info.login == ""


async def test_hub_rejected_token_never_calls_provider():
    p = AccountProvider("github", rejected=True)
    info = await hub_of(p).account("github")
    assert info.status == "token rejected" and p.account_calls == 0


async def test_hub_marks_rejected_after_failed_call():
    p = AccountProvider("github", error=ProviderError("github", "token rejected"))

    async def fail():
        p.token_rejected = True
        raise ProviderError("github", "token rejected")

    p.account = fail
    assert (await hub_of(p).account("github")).status == "token rejected"


async def test_hub_error_is_reported_not_cached():
    p = AccountProvider("github", error=ProviderError("github", "network error"))
    h = hub_of(p)
    assert (await h.account("github")).status == "error"
    await h.account("github")
    assert p.account_calls == 2


async def test_hub_rate_limit_pauses_further_calls():
    p = AccountProvider("github", error=RateLimited("github", "rate limited", None))
    h = hub_of(p)
    assert (await h.account("github")).status == "rate limited"
    assert (await h.account("github")).status == "rate limited"
    assert p.account_calls == 1


async def test_hub_unexpected_exception_is_contained():
    info = await hub_of(AccountProvider("github", error=RuntimeError("boom secret"))).account("github")
    assert info.status == "error" and "secret" not in info.message


async def test_hub_caches_for_minutes_then_refetches():
    now = [1000.0]
    p = AccountProvider("github", ProviderAccount("me"))
    h = hub_of(p, clock=lambda: now[0])
    await h.account("github")
    await h.account("github")
    assert p.account_calls == 1
    await h.account("github", refresh=True)
    assert p.account_calls == 2
    now[0] += 301
    await h.account("github")
    assert p.account_calls == 3


async def test_hub_unconfigured_host_and_all_hosts_in_order():
    h = hub_of(AccountProvider("github", ProviderAccount("me")))
    assert (await h.account("nope")).status == "not configured"
    infos = await h.accounts()
    assert [i.host for i in infos] == ["github", "gitlab", "codeberg"]
    assert [i.status for i in infos] == ["signed in", "not configured", "not configured"]


async def test_hub_account_is_not_written_to_the_database(tmp_path):
    db = str(tmp_path / "x.db")
    h = Hub({"github": AccountProvider("github", ProviderAccount("uniquelogin"))}, Cache(db), Favorites(db))
    await h.account("github")
    assert b"uniquelogin" not in (tmp_path / "x.db").read_bytes()


# ---------------------------------------------------------------- CLI, web, TUI: no token in output

def _cli(argv, hub):
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(argv, hub_factory=lambda: hub, stdout=out, stderr=err, gh_cli=lambda: None)
    return code, out.getvalue(), err.getvalue()


def _sample_hub():
    return hub_of(AccountProvider("github", ProviderAccount("me", ("repo",), RateLimit(5000, 4000))),
                  AccountProvider("gitlab", error=ProviderError("gitlab", "network error")),
                  sources={"github": "env GITHUB_TOKEN"})


def test_cli_accounts_table():
    code, out, err = _cli(["accounts"], _sample_hub())
    assert code == 3  # one host failed
    assert "signed in" in out and "me" in out and "env GITHUB_TOKEN" in out and "4000/5000" in out
    assert "gitlab: network error" in err


def test_cli_accounts_json_shape():
    code, out, _ = _cli(["accounts", "--json"], _sample_hub())
    doc = json.loads(out)
    assert doc["schema_version"] == 1
    gh = doc["accounts"][0]
    assert gh["login"] == "me" and gh["token_source"] == "env GITHUB_TOKEN" and gh["scopes"] == ["repo"]
    assert gh["rate_limit"] == {"limit": 5000, "remaining": 4000, "reset_at": None} and gh["can_star_fork"] == "yes"
    assert doc["accounts"][2]["status"] == "not configured"


def test_cli_accounts_exit_zero_when_every_host_answers():
    hub = hub_of(AccountProvider("github", None), AccountProvider("gitlab", ProviderAccount("me")),
                 AccountProvider("codeberg", None))
    assert _cli(["accounts"], hub)[0] == 0


def test_cli_accounts_output_never_holds_the_token(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", SECRET)
    from repohub.config import build_hub
    hub = build_hub()
    for p in hub.providers.values():
        async def fake(self=p):
            return ProviderAccount("me", ("repo",))
        p.account = fake
    code, out, err = _cli(["accounts", "--json"], hub)
    assert SECRET not in out + err and "env GITHUB_TOKEN" in out
    assert SECRET not in repr(hub.token_sources)


def test_web_accounts_page_escapes_and_hides_token(tmp_path):
    p = AccountProvider("github", ProviderAccount("me", ("repo",)))
    hub = hub_of(p, sources={"github": "env GITHUB_TOKEN"})
    client = TestClient(create_app(hub, tmp_path, session_token="t", shelves=[Shelf("s", topic="x")]),
                        base_url="http://localhost")
    r = client.get("/accounts")
    assert r.status_code == 200 and "env GITHUB_TOKEN" in r.text and "me" in r.text
    assert "Accounts" in client.get("/").text  # nav link
    assert client.post("/accounts").status_code == 405  # read-only page


def test_web_accounts_page_survives_hostile_error_text(tmp_path):
    p = AccountProvider("github", error=ProviderError("github", "<script>alert(1)</script>"))
    client = TestClient(create_app(hub_of(p), tmp_path, session_token="t", shelves=[Shelf("s", topic="x")]),
                        base_url="http://localhost")
    assert "<script>alert(1)</script>" not in client.get("/accounts").text


async def test_tui_accounts_screen(tmp_path):
    from textual.widgets import DataTable

    from repohub.tui.app import RepoHubApp

    hub = hub_of(AccountProvider("github", ProviderAccount("me", ("repo",))), sources={"github": "gh CLI"})
    app = RepoHubApp(hub, tmp_path, shelves=[Shelf("s", topic="x")])
    async with app.run_test() as pilot:
        await pilot.press("f2")
        await app.workers.wait_for_complete()
        await pilot.pause()
        t = app.query_one(DataTable)
        assert app.view == "accounts" and t.row_count == 3
        assert "gh CLI" in [str(c) for c in t.get_row_at(0)]


# ---------------------------------------------------------------- review follow-ups

async def test_hub_token_dropped_after_caching_shows_rejected_at_once():
    p = AccountProvider("github", ProviderAccount("me"))
    h = hub_of(p)
    assert (await h.account("github")).status == "signed in"
    p.token_rejected = True
    assert (await h.account("github")).status == "token rejected"


@respx.mock
async def test_gitlab_scope_lookup_401_does_not_drop_the_token():
    respx.get("https://gitlab.com/api/v4/user").mock(return_value=httpx.Response(200, json={"username": "t"}))
    respx.get("https://gitlab.com/api/v4/personal_access_tokens/self").mock(return_value=httpx.Response(401))
    p = GitLabProvider("tok")
    got = await p.account()
    assert got.scopes is None and not p.token_rejected and "PRIVATE-TOKEN" in p._client.headers


@respx.mock
async def test_gitlab_scope_lookup_rate_limit_is_ignored():
    respx.get("https://gitlab.com/api/v4/user").mock(return_value=httpx.Response(200, json={"username": "t"}))
    respx.get("https://gitlab.com/api/v4/personal_access_tokens/self").mock(return_value=httpx.Response(429))
    assert (await GitLabProvider("tok").account()).scopes is None


def test_cli_accounts_exit_one_when_no_host_answers():
    hub = hub_of(AccountProvider("github", error=ProviderError("github", "network error")))
    assert _cli(["accounts"], hub)[0] == 1


def test_web_accounts_page_never_shows_token_values(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", SECRET)
    from repohub.config import build_hub
    hub = build_hub()
    for p in hub.providers.values():
        async def fake(self=p):
            return ProviderAccount("me", ("repo",))
        p.account = fake
    client = TestClient(create_app(hub, tmp_path, session_token="t", shelves=[Shelf("s", topic="x")]),
                        base_url="http://localhost")
    assert SECRET not in client.get("/accounts").text


async def test_tui_accounts_key_ignored_on_detail_screen(tmp_path):
    from repohub.tui.app import DetailScreen, RepoHubApp
    from helpers import mk

    gh = AccountProvider("github", ProviderAccount("me"))
    gh.repos = [mk("github", "o/r", 5)]
    app = RepoHubApp(hub_of(gh), tmp_path, shelves=[Shelf("s", topic="x")])
    async with app.run_test() as pilot:
        app.push_screen(DetailScreen(app.hub, "github", "o/r", tmp_path, app.cloner))
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("f2")
        await pilot.pause()
        assert app.view != "accounts" and gh.account_calls == 0
