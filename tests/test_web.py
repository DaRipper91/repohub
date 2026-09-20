import pytest
from fastapi.testclient import TestClient

from helpers import FakeProvider, make_hub, mk
from repohub.core.browse import Shelf
from repohub.core.clone import CloneError
from repohub.core.models import Asset, Release
from repohub.web.app import create_app, render_markdown

TOKEN = "test-token"


@pytest.fixture
def setup(tmp_path):
    rel = Release("v1", "2026-09-01T00:00:00Z", (Asset("t-aarch64.tgz", 5, "https://x/t", "arm64"),))
    gh = FakeProvider("github", [mk("github", "o/r", 50, description="<script>alert(1)</script>")],
                      readme="# Title\n\n<script>alert(1)</script>\n\n[bad](javascript:alert(1))", release=rel)
    hub = make_hub(gh)
    calls = []

    def cloner(url, root):
        calls.append((url, root))
        return tmp_path / "r"

    app = create_app(hub, tmp_path, session_token=TOKEN, shelves=[Shelf("Terminal tools", topic="tui", min_stars=10)], cloner=cloner)
    return TestClient(app, base_url="http://localhost"), hub, calls


def test_home_lists_shelves(setup):
    client, *_ = setup
    r = client.get("/")
    assert r.status_code == 200 and "RepoHub" in r.text and "Terminal tools" in r.text


def test_shelf_fragment_renders_repos(setup):
    client, *_ = setup
    assert "o/r" in client.get("/shelf/0").text
    assert client.get("/shelf/9").status_code == 404


def test_search_escapes_descriptions(setup):
    client, *_ = setup
    r = client.get("/search", params={"q": "x"})
    assert "o/r" in r.text and "<script>alert(1)</script>" not in r.text and "&lt;script&gt;" in r.text


def test_search_rejects_bad_host(setup):
    client, *_ = setup
    assert client.get("/search", params={"q": "x", "host": "evil"}).status_code == 400


def test_repo_page_sanitizes_readme_and_shows_arm64_badge(setup):
    client, *_ = setup
    r = client.get("/repo/github/o/r")
    assert r.status_code == 200 and "Title" in r.text and "arm64" in r.text
    assert "<script>alert" not in r.text and 'href="javascript:' not in r.text.lower()


def test_repo_page_rejects_invalid_slug(setup):
    client, *_ = setup
    assert client.get("/repo/github/o/r/extra").status_code == 404
    assert client.get("/repo/nowhere/o/r").status_code == 404


def test_render_markdown_strips_scripts_and_js_links():
    out = render_markdown("<script>x</script>\n\n[a](javascript:alert(1)) **b**")
    assert "<script" not in out and 'href="javascript:' not in out.lower() and "<strong>b</strong>" in out


def test_favorite_requires_token_then_toggles(setup):
    client, hub, _ = setup
    assert client.get("/repo/github/o/r").status_code == 200
    assert client.post("/favorite", data={"host": "github", "slug": "o/r"}).status_code == 403
    assert client.post("/favorite", data={"host": "github", "slug": "o/r", "token": "wrong"}).status_code == 403
    assert client.post("/favorite", data={"host": "github", "slug": "o/r", "token": TOKEN}).status_code == 200
    assert hub.favorites.is_favorite("github:o/r")
    assert "o/r" in client.get("/favorites").text
    client.post("/favorite", data={"host": "github", "slug": "o/r", "token": TOKEN})
    assert not hub.favorites.is_favorite("github:o/r")


def test_clone_confirm_shows_destination_and_post_requires_token(setup, tmp_path):
    client, _, calls = setup
    r = client.get("/clone", params={"host": "github", "slug": "o/r"})
    assert r.status_code == 200 and str(tmp_path.resolve() / "r") in r.text
    assert client.post("/clone", data={"host": "github", "slug": "o/r"}).status_code == 403
    assert calls == []
    ok = client.post("/clone", data={"host": "github", "slug": "o/r", "token": TOKEN})
    assert ok.status_code == 200 and calls == [("https://github.com/o/r.git", tmp_path)]


def test_clone_error_is_shown_not_raised(tmp_path):
    hub = make_hub(FakeProvider("github", [mk()]))

    def cloner(url, root):
        raise CloneError("already exists")

    client = TestClient(create_app(hub, tmp_path, session_token=TOKEN, shelves=[], cloner=cloner), base_url="http://localhost")
    r = client.post("/clone", data={"host": "github", "slug": "o/r", "token": TOKEN})
    assert r.status_code == 200 and "already exists" in r.text


def test_foreign_host_header_is_rejected(setup):
    client, *_ = setup
    assert client.get("/", headers={"host": "evil.example"}).status_code == 400


def test_security_headers_present(setup):
    client, *_ = setup
    h = client.get("/").headers
    assert "script-src 'self'" in h["content-security-policy"] and "base-uri 'none'" in h["content-security-policy"] and h["x-content-type-options"] == "nosniff"


def test_static_htmx_is_served(setup):
    client, *_ = setup
    assert client.get("/static/htmx.min.js").status_code == 200


@pytest.mark.parametrize("src", ["[a](javascript:alert(1))", "[a](vbscript:x)", "[a](data:text/html,x)", "![i](javascript:x)"])
def test_markdown_dangerous_schemes_produce_no_href_or_src(src):
    out = render_markdown(src).lower()
    for scheme in ("javascript:", "vbscript:", "data:"):
        assert f'href="{scheme}' not in out and f'src="{scheme}' not in out


def test_repo_page_never_renders_unsafe_homepage_or_url(tmp_path):
    hub = make_hub(FakeProvider("github", [mk("github", "o/r", 5, homepage="javascript:alert(1)", url="javascript:alert(2)")]))
    client = TestClient(create_app(hub, tmp_path, session_token=TOKEN, shelves=[]), base_url="http://localhost")
    r = client.get("/repo/github/o/r")
    assert r.status_code == 200 and 'href="javascript:' not in r.text.lower()


def test_non_ascii_token_is_403_not_500(setup):
    client, _, calls = setup
    assert client.post("/favorite", data={"host": "github", "slug": "o/r", "token": "t\u00f6k"}).status_code == 403
    assert client.post("/clone", data={"host": "github", "slug": "o/r", "token": "t\u00f6k"}).status_code == 403
    assert calls == []


def test_days_and_min_stars_are_range_checked(setup):
    client, *_ = setup
    assert client.get("/search", params={"q": "x", "days": "99999999999999999999"}).status_code == 422
    assert client.get("/search", params={"q": "x", "days": "9999999"}).status_code == 422
    assert client.get("/search", params={"q": "x", "min_stars": "-1"}).status_code == 422
    assert client.get("/search", params={"q": "x", "days": "30"}).status_code == 200


def test_app_js_is_referenced_and_served(setup):
    client, *_ = setup
    assert '/static/app.js' in client.get("/").text
    assert client.get("/static/app.js").status_code == 200
