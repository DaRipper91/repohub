import httpx
import pytest
import respx

from helpers import FakeProvider
from repohub.core.actionlog import ActionLog
from repohub.core.cache import Cache
from repohub.core.hub import Hub
from repohub.core.providers.base import ActionDenied, Conflict, ForkResult, NotFound, ProviderError, RateLimited
from repohub.core.providers.forgejo import ForgejoProvider
from repohub.core.providers.github import GitHubProvider
from repohub.core.providers.gitlab import GitLabProvider
from repohub.core.accounts import ProviderAccount
from repohub.core.store import Favorites

GH = "https://api.github.com"
GL = "https://gitlab.com/api/v4"
CB = "https://codeberg.org/api/v1"


def forgejo(token="tok"):
    return ForgejoProvider("codeberg", CB, token)


# ---------------------------------------------------------------- GitHub

@respx.mock
async def test_github_star_unstar_state():
    put = respx.put(f"{GH}/user/starred/o/r").mock(return_value=httpx.Response(204))
    delete = respx.delete(f"{GH}/user/starred/o/r").mock(return_value=httpx.Response(204))
    get = respx.get(f"{GH}/user/starred/o/r").mock(side_effect=[httpx.Response(204), httpx.Response(404)])
    p = GitHubProvider("tok")
    await p.star("o/r")
    assert put.calls.last.request.headers["authorization"] == "Bearer tok"
    assert put.calls.last.request.headers["content-length"] == "0"
    await p.unstar("o/r")
    assert delete.call_count == 1
    assert await p.starred("o/r") is True and await p.starred("o/r") is False and get.call_count == 2


@respx.mock
async def test_github_already_starred_304_is_success():
    respx.put(f"{GH}/user/starred/o/r").mock(return_value=httpx.Response(304))
    await GitHubProvider("tok").star("o/r")


@respx.mock
async def test_github_fork():
    respx.post(f"{GH}/repos/o/r/forks").mock(return_value=httpx.Response(
        202, json={"full_name": "me/r", "html_url": "https://github.com/me/r"}))
    assert await GitHubProvider("tok").fork("o/r") == ForkResult("me/r", "https://github.com/me/r")


@respx.mock
async def test_github_fork_rejects_hostile_slug_and_url():
    respx.post(f"{GH}/repos/o/r/forks").mock(return_value=httpx.Response(
        202, json={"full_name": "me/r", "html_url": "javascript:alert(1)"}))
    assert (await GitHubProvider("tok").fork("o/r")).url == "https://github.com/me/r"
    respx.post(f"{GH}/repos/o/r/forks").mock(return_value=httpx.Response(202, json={"full_name": "<x>/..", "html_url": ""}))
    with pytest.raises(ProviderError, match="unexpected response"):
        await GitHubProvider("tok").fork("o/r")


@respx.mock
@pytest.mark.parametrize("status,exc", [(401, ActionDenied), (403, ActionDenied), (404, NotFound),
                                        (422, Conflict), (500, ProviderError), (302, ProviderError)])
async def test_github_write_errors_are_typed_and_never_retried(status, exc):
    route = respx.put(f"{GH}/user/starred/o/r").mock(return_value=httpx.Response(status, text="secret-body"))
    p = GitHubProvider("tok")
    with pytest.raises(exc) as e:
        await p.star("o/r")
    assert route.call_count == 1 and "secret-body" not in str(e.value)
    assert "Authorization" in p._client.headers and not p.token_rejected  # a write never drops the token


@respx.mock
async def test_github_write_rate_limit():
    respx.put(f"{GH}/user/starred/o/r").mock(return_value=httpx.Response(
        403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1790000000"}))
    with pytest.raises(RateLimited) as e:
        await GitHubProvider("tok").star("o/r")
    assert e.value.reset_at == 1790000000


@respx.mock
async def test_write_without_token_makes_no_request():
    route = respx.put(f"{GH}/user/starred/o/r").mock(return_value=httpx.Response(204))
    with pytest.raises(ActionDenied, match="not signed in"):
        await GitHubProvider().star("o/r")
    assert route.call_count == 0


@respx.mock
async def test_write_network_error_is_not_retried():
    route = respx.put(f"{GH}/user/starred/o/r").mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(ProviderError, match="network error"):
        await GitHubProvider("tok").star("o/r")
    assert route.call_count == 1


async def test_write_rejects_invalid_slug():
    with pytest.raises(ProviderError, match="invalid repository name"):
        await GitHubProvider("tok").star("../x")


# ---------------------------------------------------------------- Forgejo

@respx.mock
async def test_forgejo_star_unstar_state_fork():
    put = respx.put(f"{CB}/user/starred/o/r").mock(return_value=httpx.Response(204))
    respx.delete(f"{CB}/user/starred/o/r").mock(return_value=httpx.Response(204))
    respx.get(f"{CB}/user/starred/o/r").mock(return_value=httpx.Response(404))
    respx.post(f"{CB}/repos/o/r/forks").mock(return_value=httpx.Response(
        202, json={"full_name": "me/r", "html_url": "https://codeberg.org/me/r"}))
    p = forgejo()
    await p.star("o/r")
    await p.unstar("o/r")
    assert put.calls.last.request.headers["authorization"] == "token tok"
    assert await p.starred("o/r") is False
    assert (await p.fork("o/r")).slug == "me/r"


@respx.mock
async def test_forgejo_denied_and_no_token():
    respx.put(f"{CB}/user/starred/o/r").mock(return_value=httpx.Response(403))
    with pytest.raises(ActionDenied):
        await forgejo().star("o/r")
    with pytest.raises(ActionDenied, match="not signed in"):
        await forgejo(None).star("o/r")


# ---------------------------------------------------------------- GitLab

@respx.mock
async def test_gitlab_star_unstar_encoded_path():
    star = respx.post(f"{GL}/projects/g%2Fs%2Fr/star").mock(return_value=httpx.Response(200, json={}))
    respx.post(f"{GL}/projects/g%2Fs%2Fr/unstar").mock(return_value=httpx.Response(304))  # not starred: success
    p = GitLabProvider("tok")
    await p.star("g/s/r")
    await p.unstar("g/s/r")
    assert star.calls.last.request.headers["private-token"] == "tok"


@respx.mock
async def test_gitlab_starred_matches_exact_username():
    respx.get(f"{GL}/user").mock(return_value=httpx.Response(200, json={"username": "me"}))
    route = respx.get(f"{GL}/projects/g%2Fr/starrers").mock(return_value=httpx.Response(
        200, json=[{"user": {"username": "me2"}}]))
    p = GitLabProvider("tok")
    assert await p.starred("g/r") is False and route.calls.last.request.url.params["search"] == "me"
    route.mock(return_value=httpx.Response(200, json=[{"user": {"username": "me2"}}, {"user": {"username": "me"}}]))
    assert await p.starred("g/r") is True


@respx.mock
async def test_gitlab_fork_and_conflict():
    respx.post(f"{GL}/projects/g%2Fr/fork").mock(return_value=httpx.Response(
        201, json={"path_with_namespace": "me/r", "web_url": "https://gitlab.com/me/r"}))
    assert (await GitLabProvider("tok").fork("g/r")).slug == "me/r"
    respx.post(f"{GL}/projects/g%2Fr/fork").mock(return_value=httpx.Response(409, text="x"))
    with pytest.raises(Conflict):
        await GitLabProvider("tok").fork("g/r")


# ---------------------------------------------------------------- action log

def test_action_log_records_and_caps_without_secrets():
    log = ActionLog()
    for i in range(ActionLog.__init__.__globals__["MAX_ROWS"] + 5):
        log.add("github", f"o/r{i}", "star", True, "done")
    rows = log.recent(500)
    assert len(rows) == 200 and rows[0].slug.endswith("204") and rows[0].ok


def test_action_log_sanitises_and_rejects_unknown_action():
    log = ActionLog()
    log.add("github", "o/r", "fork", False, "bad\x1b[31m\n" + "x" * 500)
    e = log.recent()[0]
    assert "\x1b" not in e.result and len(e.result) <= 200
    with pytest.raises(ValueError):
        log.add("github", "o/r", "delete", True, "")


def test_action_log_persists_only_expected_columns(tmp_path):
    db = str(tmp_path / "a.db")
    ActionLog(db).add("github", "o/r", "star", True, "done")
    assert ActionLog(db).recent()[0].slug == "o/r"


# ---------------------------------------------------------------- Hub

class WriteProvider(FakeProvider):
    def __init__(self, host="github", signed_in=True, error=None):
        super().__init__(host)
        self.signed_in, self.error, self.calls_log = signed_in, error, []
        self.token_rejected = False

    async def account(self):
        return ProviderAccount("me", ("repo",)) if self.signed_in else None

    async def _do(self, name, slug):
        self.calls_log.append((name, slug))
        if self.error:
            raise self.error

    async def star(self, slug):
        await self._do("star", slug)

    async def unstar(self, slug):
        await self._do("unstar", slug)

    async def starred(self, slug):
        await self._do("starred", slug)
        return True

    async def fork(self, slug):
        await self._do("fork", slug)
        return ForkResult("me/r", "https://github.com/me/r")


def mkhub(p):
    return Hub({p.host: p}, Cache(), Favorites())


async def test_hub_star_logs_and_drops_cached_repo():
    p = WriteProvider()
    h = mkhub(p)
    h.cache.set("repo:github@github.com:o/r", {"x": 1}, 999)
    await h.set_star("github", "o/r", True)
    assert p.calls_log == [("star", "o/r")] and h.cache.get("repo:github@github.com:o/r") is None
    e = h.recent_actions()[0]
    assert (e.host, e.slug, e.action, e.ok) == ("github", "o/r", "star", True)


async def test_hub_requires_sign_in_and_makes_no_write():
    p = WriteProvider(signed_in=False)
    h = mkhub(p)
    with pytest.raises(ActionDenied):
        await h.set_star("github", "o/r", True)
    assert p.calls_log == [] and h.recent_actions()[0].ok is False


async def test_hub_failure_is_logged_and_not_retried():
    p = WriteProvider(error=ProviderError("github", "HTTP 500"))
    h = mkhub(p)
    with pytest.raises(ProviderError):
        await h.fork("github", "o/r")
    assert p.calls_log == [("fork", "o/r")]
    assert h.recent_actions()[0].result == "HTTP 500" and not h.recent_actions()[0].ok


async def test_hub_rate_limit_pauses_further_writes():
    p = WriteProvider(error=RateLimited("github", "rate limited", None))
    h = mkhub(p)
    with pytest.raises(RateLimited):
        await h.set_star("github", "o/r", True)
    with pytest.raises(RateLimited):
        await h.set_star("github", "o/r", False)
    assert p.calls_log == [("star", "o/r")]


async def test_hub_fork_result_and_log():
    h = mkhub(WriteProvider())
    assert (await h.fork("github", "o/r")).slug == "me/r"
    assert "me/r" in h.recent_actions()[0].result


async def test_hub_rejects_bad_slug_unconfigured_host_and_unexpected_errors():
    p = WriteProvider()
    h = mkhub(p)
    for host, slug in (("github", "../x"), ("nope", "o/r"), ("gitlab", "o/r")):
        with pytest.raises(ProviderError):
            await h.set_star(host, slug, True)
    assert p.calls_log == []
    p.error = RuntimeError("secret detail")
    with pytest.raises(ProviderError, match="unexpected error"):
        await h.set_star("github", "o/r", True)


async def test_hub_starred_never_raises():
    p = WriteProvider()
    h = mkhub(p)
    assert await h.starred("github", "o/r") is True
    p.error = ProviderError("github", "x")
    assert await h.starred("github", "o/r") is None
    assert await mkhub(WriteProvider(signed_in=False)).starred("github", "o/r") is None
