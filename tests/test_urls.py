import httpx
import pytest
import respx

from repohub.core.models import SearchFilters
from repohub.core.providers.base import safe_url
from repohub.core.providers.github import GitHubProvider
from repohub.core.providers.gitlab import GitLabProvider


@pytest.mark.parametrize("bad", ["javascript:alert(1)", "data:text/html,x", "vbscript:x", "file:///etc/passwd",
                                 "https://", "https://a.example/x y", "https://a.example/x\ny", "https://a.example/\x00",
                                 "//a.example", None, "", "   "])
def test_safe_url_rejects(bad):
    assert safe_url(bad) == ""


def test_safe_url_accepts_http_and_https():
    assert safe_url("HTTP://ok.example/x") == "HTTP://ok.example/x"
    assert safe_url("  https://ok.example/x ") == "https://ok.example/x"


GH = {"full_name": "o/r", "html_url": "https://github.com/o/r", "description": "d", "stargazers_count": 5,
      "language": "Rust", "license": None, "topics": [], "pushed_at": "", "archived": False, "forks_count": 0,
      "homepage": "javascript:alert(1)"}
GL = {"id": 7, "path_with_namespace": "g/p", "web_url": "javascript:alert(1)", "star_count": 1}


@respx.mock
async def test_github_unsafe_homepage_and_url():
    item = dict(GH, html_url="data:text/html,x")
    respx.get("https://api.github.com/search/repositories").mock(return_value=httpx.Response(200, json={"items": [item]}))
    r = (await GitHubProvider().search("x", SearchFilters()))[0]
    assert r.homepage == "" and r.url == "https://github.com/o/r"


@respx.mock
async def test_gitlab_unsafe_url_falls_back():
    respx.get("https://gitlab.com/api/v4/projects").mock(return_value=httpx.Response(200, json=[GL]))
    r = (await GitLabProvider().search("x", SearchFilters()))[0]
    assert r.url == "https://gitlab.com/g/p"


@pytest.mark.parametrize("bad", [
    "https://a@b.example/", "https://a:b@b.example/x", "https://a.example.org@evil.example/o/r",
    "https://a.example/o‮r", "https://a.example/o​r", "https://a.example/o­r",
    "https://a.example/ x", "https://a.example/⁦x", "https://a.example/\x85x",
    "https://a​.example/x", "https://a.example/﻿x"])
def test_safe_url_rejects_userinfo_and_invisible_characters(bad):
    assert safe_url(bad) == ""


def test_safe_url_still_accepts_ordinary_urls_with_ports_and_unicode_letters():
    assert safe_url("https://ok.example:8443/a?b=c#d") == "https://ok.example:8443/a?b=c#d"
    assert safe_url("https://ok.example/café") == "https://ok.example/café"
