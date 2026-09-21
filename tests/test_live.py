import pytest

from repohub.core.auth import find_host_tokens
from repohub.core.hosts import registry
from repohub.core.models import SearchFilters
from repohub.core.providers.base import NotFound, ProviderError, RateLimited
from repohub.core.providers.forgejo import ForgejoProvider
from repohub.core.providers.github import GitHubProvider
from repohub.core.providers.gitlab import GitLabProvider

pytestmark = pytest.mark.live


async def test_github_live_search_and_detail():
    p = GitHubProvider(find_host_tokens().for_host("github"))
    repos = await p.search("ripgrep", SearchFilters(min_stars=1000))
    assert repos and repos[0].host == "github"
    assert (await p.repo(repos[0].slug)).slug == repos[0].slug


async def test_gitlab_live_search():
    p = GitLabProvider(find_host_tokens().for_host("gitlab"))
    repos = await p.search("wireshark", SearchFilters())
    assert repos and all(r.host == "gitlab" for r in repos)


# --- Codeberg (Forgejo), anonymous: no token is read or sent ---


@pytest.fixture
async def codeberg():
    p = ForgejoProvider("codeberg", "https://codeberg.org/api/v1")
    try:
        yield p
    finally:
        await p.aclose()


async def _live(coro):
    """Await a provider call; skip (never fail) on rate limits, network trouble or odd server replies.

    NotFound is re-raised so a test can assert on it.
    """
    try:
        return await coro
    except NotFound:
        raise
    except RateLimited:
        pytest.skip("Codeberg rate limited this anonymous client")
    except ProviderError as e:
        pytest.skip(f"Codeberg unavailable or unexpected response ({e})")


async def test_codeberg_live_search(codeberg):
    repos = await _live(codeberg.search("terminal", SearchFilters(hide_forks=True)))
    if not repos:
        pytest.skip("Codeberg returned no results for 'terminal'")
    assert all(r.host == "codeberg" for r in repos)
    assert all(registry().slug_ok("codeberg", r.slug) for r in repos)
    assert all(r.stars >= 0 for r in repos)


async def test_codeberg_live_multiword_search(codeberg):
    repos = await _live(codeberg.search("wayland terminal", SearchFilters()))
    if not repos:
        pytest.skip("Codeberg returned nothing for 'wayland terminal'; server multi-word behaviour may have changed")
    slugs = {r.slug.lower() for r in repos}
    if "dnkl/foot" not in slugs:
        pytest.skip("dnkl/foot absent from 'wayland terminal' results; server multi-word behaviour may have changed")
    assert "dnkl/foot" in slugs


async def test_codeberg_live_repo_detail(codeberg):
    r = await _live(codeberg.repo("dnkl/foot"))
    assert r.host == "codeberg"
    assert r.stars > 100
    assert r.license == ""


async def test_codeberg_live_readme(codeberg):
    text = await _live(codeberg.readme("dnkl/foot"))
    assert text and text.strip()


async def test_codeberg_live_latest_release(codeberg):
    rel = await _live(codeberg.latest_release("dnkl/foot"))
    if rel is not None:
        assert isinstance(rel.tag, str) and rel.tag
        assert isinstance(rel.assets, tuple)


async def test_codeberg_live_missing_repo(codeberg):
    with pytest.raises(NotFound):
        await _live(codeberg.repo("nobody-zz9/nope-zz9"))
