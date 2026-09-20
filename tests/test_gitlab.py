import httpx
import pytest
import respx

from repohub.core.models import SearchFilters
from repohub.core.providers.base import ProviderError, RateLimited
from repohub.core.providers.gitlab import GitLabProvider

API = "https://gitlab.com/api/v4"
ITEM = {
    "id": 7, "path_with_namespace": "g/sub/p", "web_url": "https://gitlab.com/g/sub/p", "description": None,
    "star_count": 42, "topics": ["cli"], "last_activity_at": "2026-09-03T00:00:00Z", "archived": False,
    "forks_count": 3, "license": {"name": "MIT License"},
}


@respx.mock
async def test_search_params_and_mapping():
    route = respx.get(f"{API}/projects").mock(return_value=httpx.Response(200, json=[ITEM]))
    repos = await GitLabProvider().search(
        "cli", SearchFilters(language="Rust", topic="cli", pushed_after="2026-01-01"))
    p = route.calls.last.request.url.params
    assert p["search"] == "cli" and p["order_by"] == "star_count" and p["archived"] == "false"
    assert p["with_programming_language"] == "Rust" and p["topic"] == "cli"
    assert p["last_activity_after"].startswith("2026-01-01")
    r = repos[0]
    assert (r.host, r.slug, r.stars, r.description, r.license) == ("gitlab", "g/sub/p", 42, "", "MIT License")
    assert r.language == "Rust"


@respx.mock
async def test_private_token_header():
    route = respx.get(f"{API}/projects").mock(return_value=httpx.Response(200, json=[]))
    await GitLabProvider(token="tok").search("x", SearchFilters())
    assert route.calls.last.request.headers["private-token"] == "tok"


@respx.mock
async def test_rate_limit_and_errors():
    respx.get(f"{API}/projects").mock(return_value=httpx.Response(429, headers={"retry-after": "30"}))
    with pytest.raises(RateLimited):
        await GitLabProvider().search("x", SearchFilters())
    respx.get(f"{API}/projects").mock(return_value=httpx.Response(401))
    with pytest.raises(ProviderError, match="token rejected"):
        await GitLabProvider(token="bad").search("x", SearchFilters())
    respx.get(f"{API}/projects").mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(ProviderError, match="network"):
        await GitLabProvider().search("x", SearchFilters())


@respx.mock
async def test_repo_uses_top_language_and_encoded_slug():
    route = respx.get(f"{API}/projects/g%2Fsub%2Fp").mock(return_value=httpx.Response(200, json=ITEM))
    respx.get(f"{API}/projects/7/languages").mock(return_value=httpx.Response(200, json={"Go": 20.0, "Rust": 80.0}))
    r = await GitLabProvider().repo("g/sub/p")
    assert r.language == "Rust" and route.called


@respx.mock
async def test_readme_tries_candidates_until_found():
    respx.get(f"{API}/projects/g%2Fp/repository/files/README.md/raw").mock(return_value=httpx.Response(404))
    respx.get(f"{API}/projects/g%2Fp/repository/files/README.markdown/raw").mock(return_value=httpx.Response(404))
    respx.get(f"{API}/projects/g%2Fp/repository/files/README.rst/raw").mock(return_value=httpx.Response(200, text="rst"))
    assert await GitLabProvider().readme("g/p") == "rst"


@respx.mock
async def test_latest_release_maps_link_assets():
    respx.get(f"{API}/projects/g%2Fp/releases").mock(return_value=httpx.Response(200, json=[{
        "tag_name": "v2", "released_at": "2026-09-04T00:00:00Z",
        "assets": {"links": [{"name": "t-arm64.zip", "url": "https://x/t", "direct_asset_url": "https://x/d"}]}}]))
    rel = await GitLabProvider().latest_release("g/p")
    assert rel.tag == "v2" and rel.has_arm64 and rel.assets[0].url == "https://x/d"


@respx.mock
async def test_no_releases_is_none_and_bad_slug_rejected():
    respx.get(f"{API}/projects/g%2Fp/releases").mock(return_value=httpx.Response(200, json=[]))
    assert await GitLabProvider().latest_release("g/p") is None
    with pytest.raises(ProviderError, match="invalid"):
        await GitLabProvider().repo("g/../x")


@respx.mock
async def test_html_200_body_is_provider_error():
    respx.get(f"{API}/projects").mock(return_value=httpx.Response(200, text="<html>hi</html>"))
    with pytest.raises(ProviderError, match="unexpected response"):
        await GitLabProvider().search("x", SearchFilters())


@respx.mock
async def test_item_missing_slug_is_provider_error():
    bad = {k: v for k, v in ITEM.items() if k != "path_with_namespace"}
    respx.get(f"{API}/projects").mock(return_value=httpx.Response(200, json=[bad]))
    with pytest.raises(ProviderError):
        await GitLabProvider().search("x", SearchFilters())


@respx.mock
async def test_search_404_is_provider_error():
    respx.get(f"{API}/projects").mock(return_value=httpx.Response(404))
    with pytest.raises(ProviderError):
        await GitLabProvider().search("x", SearchFilters())


@respx.mock
async def test_redirect_is_provider_error_not_followed():
    respx.get(f"{API}/projects").mock(
        return_value=httpx.Response(302, headers={"location": "https://evil.example/"}))
    with pytest.raises(ProviderError, match="HTTP 302"):
        await GitLabProvider(token="tok").search("x", SearchFilters())


@respx.mock
async def test_readme_redirect_is_error_and_stops():
    respx.get(f"{API}/projects/g%2Fp/repository/files/README.md/raw").mock(
        return_value=httpx.Response(302, headers={"location": "https://evil.example/"}))
    with pytest.raises(ProviderError, match="HTTP 302"):
        await GitLabProvider().readme("g/p")


@respx.mock
async def test_repo_survives_malformed_languages():
    respx.get(f"{API}/projects/g%2Fsub%2Fp").mock(return_value=httpx.Response(200, json=ITEM))
    respx.get(f"{API}/projects/7/languages").mock(return_value=httpx.Response(200, json=["Rust"]))
    r = await GitLabProvider().repo("g/sub/p")
    assert r.language == ""
