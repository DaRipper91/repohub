import pytest

from repohub.core.auth import find_tokens
from repohub.core.models import SearchFilters
from repohub.core.providers.github import GitHubProvider
from repohub.core.providers.gitlab import GitLabProvider

pytestmark = pytest.mark.live


async def test_github_live_search_and_detail():
    p = GitHubProvider(find_tokens().github)
    repos = await p.search("ripgrep", SearchFilters(min_stars=1000))
    assert repos and repos[0].host == "github"
    assert (await p.repo(repos[0].slug)).slug == repos[0].slug


async def test_gitlab_live_search():
    p = GitLabProvider(find_tokens().gitlab)
    repos = await p.search("wireshark", SearchFilters())
    assert repos and all(r.host == "gitlab" for r in repos)
