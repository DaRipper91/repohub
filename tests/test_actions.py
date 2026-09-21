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
    assert await h.starred("github", "o/r") is True  # cached for a minute
    assert await mkhub(p).starred("github", "o/r") is None
    assert await mkhub(WriteProvider(signed_in=False)).starred("github", "o/r") is None


async def test_hub_star_state_cache_is_dropped_by_an_action():
    p = WriteProvider()
    h = mkhub(p)
    await h.starred("github", "o/r")
    await h.starred("github", "o/r")
    assert p.calls_log == [("starred", "o/r")]
    await h.set_star("github", "o/r", False)
    await h.starred("github", "o/r")
    assert [c[0] for c in p.calls_log] == ["starred", "unstar", "starred"]


# ---------------------------------------------------------------- web

from fastapi.testclient import TestClient

from helpers import mk
from repohub.core.browse import Shelf
from repohub.web.app import create_app

TOKEN = "sess-token"


class WebProvider(WriteProvider):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.repos = [mk("github", "o/r", 5)]


def web(p, tmp_path):
    h = mkhub(p)
    app = create_app(h, tmp_path, session_token=TOKEN, shelves=[Shelf("s", topic="x")])
    return TestClient(app, base_url="http://localhost"), h


def test_get_routes_never_write(tmp_path):
    p = WebProvider()
    c, _ = web(p, tmp_path)
    for url in ("/star?host=github&slug=o/r&action=star", "/star?host=github&slug=o/r&action=unstar",
                "/fork?host=github&slug=o/r"):
        r = c.get(url)
        assert r.status_code == 200 and "Confirm" in r.text and "me" in r.text
    assert [x for x in p.calls_log if x[0] in ("star", "unstar", "fork")] == []


def test_confirm_pages_name_host_repo_account_and_fork_warns(tmp_path):
    c, _ = web(WebProvider(), tmp_path)
    t = c.get("/fork?host=github&slug=o/r").text
    assert "github" in t and "o/r" in t and "me" in t and "never deletes" in t


def test_post_requires_session_token(tmp_path):
    p = WebProvider()
    c, _ = web(p, tmp_path)
    for path, data in (("/star", {"host": "github", "slug": "o/r", "action": "star"}),
                       ("/fork", {"host": "github", "slug": "o/r"})):
        assert c.post(path, data=data).status_code == 403
        assert c.post(path, data={**data, "token": "wrong"}).status_code == 403
    assert [x for x in p.calls_log if x[0] in ("star", "fork")] == []


def test_post_star_and_fork_succeed_and_are_logged(tmp_path):
    p = WebProvider()
    c, h = web(p, tmp_path)
    r = c.post("/star", data={"host": "github", "slug": "o/r", "action": "star", "token": TOKEN})
    assert r.status_code == 200 and "Starred" in r.text
    r = c.post("/fork", data={"host": "github", "slug": "o/r", "token": TOKEN})
    assert r.status_code == 200 and "me/r" in r.text
    assert [e.action for e in h.recent_actions()] == ["fork", "star"]
    assert "fork" in c.get("/accounts").text


def test_post_rejects_unknown_action_and_bad_slug(tmp_path):
    p = WebProvider()
    c, _ = web(p, tmp_path)
    assert c.post("/star", data={"host": "github", "slug": "o/r", "action": "delete", "token": TOKEN}).status_code == 404
    assert c.post("/star", data={"host": "github", "slug": "../x", "action": "star", "token": TOKEN}).status_code == 404
    assert c.get("/star?host=github&slug=o/r&action=delete").status_code == 404
    assert p.calls_log == []


@pytest.mark.parametrize("error,status", [(ActionDenied("github", "no"), 403), (RateLimited("github", "slow", None), 429),
                                          (NotFound("github", "gone"), 404), (Conflict("github", "taken"), 409),
                                          (ProviderError("github", "HTTP 500"), 502)])
def test_post_failures_map_to_statuses(tmp_path, error, status):
    p = WebProvider(error=error)
    c, _ = web(p, tmp_path)
    r = c.post("/star", data={"host": "github", "slug": "o/r", "action": "star", "token": TOKEN})
    assert r.status_code == status
    assert [x[0] for x in p.calls_log].count("star") == 1  # one attempt only


def test_not_signed_in_gets_no_confirmation_and_no_buttons(tmp_path):
    p = WebProvider(signed_in=False)
    c, _ = web(p, tmp_path)
    assert c.get("/star?host=github&slug=o/r&action=star").status_code == 403
    page_ = c.get("/repo/github/o/r").text
    assert "Star…" not in page_ and "Fork…" not in page_


def test_repo_page_shows_star_state_when_signed_in(tmp_path):
    c, _ = web(WebProvider(), tmp_path)
    t = c.get("/repo/github/o/r").text
    assert "Unstar…" in t and "Fork…" in t  # WriteProvider.starred() says True


def test_action_log_escapes_hostile_text(tmp_path):
    p = WebProvider(error=ProviderError("github", "<script>alert(1)</script>"))
    c, _ = web(p, tmp_path)
    c.post("/star", data={"host": "github", "slug": "o/r", "action": "star", "token": TOKEN})
    assert "<script>alert(1)</script>" not in c.get("/accounts").text


# ---------------------------------------------------------------- terminal app

async def _open_detail(app, pilot):
    from repohub.tui.app import DetailScreen

    app.push_screen(DetailScreen(app.hub, "github", "o/r", app.clone_root, app.cloner))
    await app.workers.wait_for_complete()
    await pilot.pause()


def tui(p, tmp_path):
    from repohub.tui.app import RepoHubApp

    h = mkhub(p)
    return RepoHubApp(h, tmp_path, shelves=[Shelf("s", topic="x")]), h


async def test_tui_star_needs_confirmation_and_n_cancels(tmp_path):
    from repohub.tui.app import ConfirmWrite

    p = WebProvider()
    app, h = tui(p, tmp_path)
    async with app.run_test() as pilot:
        await _open_detail(app, pilot)
        await pilot.press("s")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen, ConfirmWrite) and "Unstar o/r" in app.screen.text and "Account: me" in app.screen.text
        assert [x for x in p.calls_log if x[0] in ("star", "unstar")] == []
        await pilot.press("n")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert [x for x in p.calls_log if x[0] in ("star", "unstar")] == [] and h.recent_actions() == []


async def test_tui_star_confirmed_once_and_logged(tmp_path):
    p = WebProvider()
    app, h = tui(p, tmp_path)
    async with app.run_test() as pilot:
        await _open_detail(app, pilot)
        await pilot.press("s")
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("y")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert [x for x in p.calls_log if x[0] == "unstar"] == [("unstar", "o/r")]
        assert h.recent_actions()[0].action == "unstar" and h.recent_actions()[0].ok


async def test_tui_fork_confirmed_and_failure_not_retried(tmp_path):
    p = WebProvider(error=None)
    app, h = tui(p, tmp_path)
    async with app.run_test() as pilot:
        await _open_detail(app, pilot)
        p.error = ProviderError("github", "HTTP 500")
        await pilot.press("k")
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("y")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert [x for x in p.calls_log if x[0] == "fork"] == [("fork", "o/r")]
        assert not h.recent_actions()[0].ok


async def test_tui_not_signed_in_shows_no_confirmation(tmp_path):
    from repohub.tui.app import ConfirmWrite

    p = WebProvider(signed_in=False)
    app, _ = tui(p, tmp_path)
    async with app.run_test() as pilot:
        await _open_detail(app, pilot)
        await pilot.press("s")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert not isinstance(app.screen, ConfirmWrite)


def test_cli_has_no_write_commands():
    import io

    import repohub.cli as cli

    for cmd in ("star", "unstar", "fork"):
        with pytest.raises(SystemExit) as e:
            cli.main([cmd, "github:o/r"], hub_factory=lambda: pytest.fail("no hub"), stdout=io.StringIO(),
                     stderr=io.StringIO())
        assert e.value.code == 2


# ---------------------------------------------------------------- review follow-ups

@respx.mock
async def test_403_with_retry_after_is_a_rate_limit_not_a_permission_problem():
    respx.put(f"{GH}/user/starred/o/r").mock(return_value=httpx.Response(403, headers={"retry-after": "60"}))
    with pytest.raises(RateLimited):
        await GitHubProvider("tok").star("o/r")


@respx.mock
async def test_redirects_are_not_followed_by_writes():
    hop = respx.get("https://evil.example/x").mock(return_value=httpx.Response(204))
    respx.put(f"{GH}/user/starred/o/r").mock(return_value=httpx.Response(307, headers={"location": "https://evil.example/x"}))
    with pytest.raises(ProviderError, match="HTTP 307"):
        await GitHubProvider("tok").star("o/r")
    assert hop.call_count == 0


async def test_hub_cancelled_write_is_logged_and_cache_dropped():
    import asyncio

    class Slow(WriteProvider):
        async def star(self, slug):
            await asyncio.sleep(30)

    h = mkhub(Slow())
    h.cache.set("repo:github@github.com:o/r", {"x": 1}, 999)
    task = asyncio.ensure_future(h.set_star("github", "o/r", True))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert "cancelled" in h.recent_actions()[0].result and h.cache.get("repo:github@github.com:o/r") is None


async def test_hub_broken_log_does_not_turn_success_into_error():
    class Broken(ActionLog):
        def add(self, *a, **k):
            raise RuntimeError("database is locked")

    p = WriteProvider()
    h = Hub({"github": p}, Cache(), Favorites(), actions=Broken())
    h.cache.set("repo:github@github.com:o/r", {"x": 1}, 999)
    await h.set_star("github", "o/r", True)  # must not raise
    assert h.cache.get("repo:github@github.com:o/r") is None
    p.error = ProviderError("github", "HTTP 500")
    with pytest.raises(ProviderError, match="HTTP 500"):  # the real error is not masked by the log failure
        await h.set_star("github", "o/r", True)


async def test_tui_double_press_stacks_one_confirmation(tmp_path):
    from repohub.tui.app import ConfirmWrite

    p = WebProvider()
    app, _ = tui(p, tmp_path)
    async with app.run_test() as pilot:
        await _open_detail(app, pilot)
        await pilot.press("k", "k")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen, ConfirmWrite) and len(app.screen_stack) == 3  # default + detail + one modal
        await pilot.press("n")
        await pilot.pause()
        await pilot.press("k")  # allowed again after cancelling
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen, ConfirmWrite)


def test_confirm_form_disables_its_button_while_sending(tmp_path):
    c, _ = web(WebProvider(), tmp_path)
    assert 'hx-disabled-elt="find button"' in c.get("/fork?host=github&slug=o/r").text
