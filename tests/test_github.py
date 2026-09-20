import httpx
import pytest
import respx

from repohub.core.models import SearchFilters
from repohub.core.providers.base import ProviderError, RateLimited
from repohub.core.providers.github import GitHubProvider

API = "https://api.github.com"
ITEM = {
    "full_name": "o/r", "html_url": "https://github.com/o/r", "description": "d", "stargazers_count": 5,
    "language": "Rust", "license": {"spdx_id": "MIT"}, "topics": ["tui"], "pushed_at": "2026-09-01T00:00:00Z",
    "archived": False, "forks_count": 1, "homepage": None,
}


@respx.mock
async def test_search_builds_query_and_maps_repo():
    route = respx.get(f"{API}/search/repositories").mock(return_value=httpx.Response(200, json={"items": [ITEM]}))
    repos = await GitHubProvider().search(
        "tui", SearchFilters(language="Rust", min_stars=10, topic="cli", pushed_after="2026-01-01"))
    q = route.calls.last.request.url.params["q"]
    for part in ("tui", "stars:>=10", 'language:"Rust"', "topic:cli", "pushed:>=2026-01-01", "archived:false"):
        assert part in q
    r = repos[0]
    assert (r.host, r.slug, r.license, r.homepage, r.topics) == ("github", "o/r", "MIT", "", ("tui",))


@respx.mock
async def test_archived_qualifier_omitted_when_included():
    route = respx.get(f"{API}/search/repositories").mock(return_value=httpx.Response(200, json={"items": []}))
    await GitHubProvider().search("x", SearchFilters(include_archived=True))
    assert "archived:false" not in route.calls.last.request.url.params["q"]


@respx.mock
async def test_token_sent_only_when_present():
    route = respx.get(f"{API}/search/repositories").mock(return_value=httpx.Response(200, json={"items": []}))
    await GitHubProvider(token="tok").search("x", SearchFilters())
    assert route.calls.last.request.headers["authorization"] == "Bearer tok"
    await GitHubProvider().search("x", SearchFilters())
    assert "authorization" not in route.calls.last.request.headers


@respx.mock
async def test_rate_limit_raises_with_reset_time():
    respx.get(f"{API}/search/repositories").mock(return_value=httpx.Response(
        403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1790000000"}, json={}))
    with pytest.raises(RateLimited) as e:
        await GitHubProvider().search("x", SearchFilters())
    assert e.value.reset_at == 1790000000


@respx.mock
async def test_bad_token_and_network_errors_are_provider_errors():
    respx.get(f"{API}/search/repositories").mock(return_value=httpx.Response(401, json={}))
    with pytest.raises(ProviderError, match="token rejected"):
        await GitHubProvider(token="bad").search("x", SearchFilters())
    respx.get(f"{API}/search/repositories").mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(ProviderError, match="network"):
        await GitHubProvider().search("x", SearchFilters())


@respx.mock
async def test_repo_readme_and_release():
    respx.get(f"{API}/repos/o/r").mock(return_value=httpx.Response(200, json=ITEM))
    respx.get(f"{API}/repos/o/r/readme").mock(return_value=httpx.Response(200, text="# Hi"))
    respx.get(f"{API}/repos/o/r/releases/latest").mock(return_value=httpx.Response(200, json={
        "tag_name": "v1", "published_at": "2026-09-02T00:00:00Z",
        "assets": [{"name": "t-aarch64.tgz", "size": 9, "browser_download_url": "https://x/t"}]}))
    p = GitHubProvider()
    assert (await p.repo("o/r")).stars == 5
    assert await p.readme("o/r") == "# Hi"
    rel = await p.latest_release("o/r")
    assert rel.tag == "v1" and rel.has_arm64


@respx.mock
async def test_missing_readme_and_release_are_none():
    respx.get(f"{API}/repos/o/r/readme").mock(return_value=httpx.Response(404))
    respx.get(f"{API}/repos/o/r/releases/latest").mock(return_value=httpx.Response(404))
    p = GitHubProvider()
    assert await p.readme("o/r") is None
    assert await p.latest_release("o/r") is None


@respx.mock
async def test_invalid_slug_never_reaches_network():
    p = GitHubProvider()
    for bad in ("../etc/passwd", "o/r/extra", "o", "o/../x", "o/r?x=1", "o/r\n", "o/..\n"):
        with pytest.raises(ProviderError, match="invalid"):
            await p.repo(bad)
    assert respx.calls.call_count == 0


@respx.mock
async def test_html_200_body_is_provider_error():
    respx.get(f"{API}/search/repositories").mock(return_value=httpx.Response(200, text="<html>hi</html>"))
    with pytest.raises(ProviderError, match="unexpected response"):
        await GitHubProvider().search("x", SearchFilters())


@respx.mock
async def test_item_missing_slug_is_provider_error():
    bad = {k: v for k, v in ITEM.items() if k != "full_name"}
    respx.get(f"{API}/search/repositories").mock(return_value=httpx.Response(200, json={"items": [bad]}))
    with pytest.raises(ProviderError):
        await GitHubProvider().search("x", SearchFilters())


@respx.mock
async def test_search_404_is_provider_error():
    respx.get(f"{API}/search/repositories").mock(return_value=httpx.Response(404))
    with pytest.raises(ProviderError):
        await GitHubProvider().search("x", SearchFilters())


@respx.mock
async def test_redirect_is_provider_error_not_followed():
    respx.get(f"{API}/search/repositories").mock(
        return_value=httpx.Response(302, headers={"location": "https://evil.example/"}))
    with pytest.raises(ProviderError, match="HTTP 302"):
        await GitHubProvider().search("x", SearchFilters())


@respx.mock
async def test_release_asset_missing_download_url_is_provider_error():
    respx.get(f"{API}/repos/o/r/releases/latest").mock(return_value=httpx.Response(200, json={
        "tag_name": "v1", "published_at": None, "assets": [{"name": "a.zip", "size": 1}]}))
    with pytest.raises(ProviderError):
        await GitHubProvider().latest_release("o/r")


@respx.mock
async def test_untrusted_text_is_cleaned():
    item = dict(ITEM, description="hi\x1b[2J‮ there", language="Ru\x1bst", topics=["t\x00ui"])
    respx.get(f"{API}/search/repositories").mock(return_value=httpx.Response(200, json={"items": [item]}))
    r = (await GitHubProvider().search("x", SearchFilters()))[0]
    assert r.description == "hi[2J there" and r.language == "Rust" and r.topics == ("tui",)


@respx.mock
async def test_readme_and_release_text_cleaned():
    respx.get(f"{API}/repos/o/r/readme").mock(return_value=httpx.Response(200, text="# Hi\x1b[2J\nbody\x9b"))
    respx.get(f"{API}/repos/o/r/releases/latest").mock(return_value=httpx.Response(200, json={
        "tag_name": "v1\x1b", "published_at": None,
        "assets": [{"name": "a\x1b-arm64.tgz", "size": 1, "browser_download_url": "https://x/y"}]}))
    p = GitHubProvider()
    assert await p.readme("o/r") == "# Hi[2J\nbody"
    rel = await p.latest_release("o/r")
    assert rel.tag == "v1" and rel.assets[0].name == "a-arm64.tgz"


@respx.mock
async def test_rejected_token_falls_back_to_anonymous_once():
    calls = []

    def handler(request):
        calls.append(request.headers.get("authorization"))
        return httpx.Response(401, json={}) if "authorization" in request.headers else httpx.Response(200, json={"items": []})

    respx.get(f"{API}/search/repositories").mock(side_effect=handler)
    p = GitHubProvider(token="tok")
    assert await p.search("x", SearchFilters()) == []
    assert calls == ["Bearer tok", None]
    assert p.token_rejected is True
    await p.search("x", SearchFilters())
    assert calls == ["Bearer tok", None, None]


@respx.mock
async def test_anonymous_401_still_raises_and_does_not_loop():
    route = respx.get(f"{API}/search/repositories").mock(return_value=httpx.Response(401, json={}))
    p = GitHubProvider(token="tok")
    with pytest.raises(ProviderError, match="token rejected"):
        await p.search("x", SearchFilters())
    assert route.call_count == 2
    with pytest.raises(ProviderError, match="token rejected"):
        await GitHubProvider().search("x", SearchFilters())


@respx.mock
async def test_repo_404_is_not_found():
    from repohub.core.providers.base import NotFound
    respx.get(f"{API}/repos/o/r").mock(return_value=httpx.Response(404))
    with pytest.raises(NotFound, match="repository not found"):
        await GitHubProvider().repo("o/r")
