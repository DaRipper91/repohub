import pytest
from fastapi.testclient import TestClient

from helpers import FakeProvider, make_hub, mk
from repohub.core.browse import Shelf
from repohub.core.clone import CloneError
from repohub.core.models import Asset, Release
from repohub.core.providers.base import ProviderError
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


def test_search_form_has_labelled_numeric_filters_and_no_bare_zeros(setup):
    client, *_ = setup
    r = client.get("/search", params={"q": "x"})
    assert 'placeholder="min' in r.text and 'placeholder="days"' in r.text
    assert 'name="min_stars"' in r.text and 'value="0"' not in r.text


def test_repo_page_layout_lets_the_sidebar_shrink(setup):
    client, *_ = setup
    css = "".join(client.get("/static/app.css").text.split())  # ignore formatting
    assert "minmax(0,1fr)" in css and ".layout>*{min-width:0;}" in css


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


def test_empty_numeric_filters_are_accepted(setup):
    client, *_ = setup
    r = client.get("/search", params={"q": "o", "min_stars": "", "days": "", "language": "", "host": "both"})
    assert r.status_code == 200 and "o/r" in r.text
    assert client.get("/search", params={"q": "o", "min_stars": "  ", "days": " "}).status_code == 200


def test_garbage_numeric_filters_are_422_html(setup):
    client, *_ = setup
    for params in ({"min_stars": "abc"}, {"days": "1.5"}, {"days": "x"}):
        r = client.get("/search", params={"q": "o", **params})
        assert r.status_code == 422 and r.headers["content-type"].startswith("text/html")


def test_http_errors_render_html_messages_with_security_headers(setup):
    client, *_ = setup
    r = client.post("/favorite", data={"host": "github", "slug": "o/r"})
    assert r.status_code == 403
    assert r.headers["content-type"].startswith("text/html")
    assert "missing or invalid session token" in r.text
    assert "Content-Security-Policy" in r.headers
    r = client.get("/shelf/9")
    assert r.status_code == 404 and r.headers["content-type"].startswith("text/html")


def _not_found_setup(tmp_path):
    from repohub.core.providers.base import NotFound
    gh = FakeProvider("github", [], detail_error=NotFound("github", "repository not found"))
    app = create_app(make_hub(gh), tmp_path, session_token=TOKEN, shelves=[])
    return TestClient(app, base_url="http://localhost")


def test_missing_repo_page_is_404(tmp_path):
    r = _not_found_setup(tmp_path).get("/repo/github/o/gone")
    assert r.status_code == 404 and "repository not found" in r.text


def test_other_provider_errors_stay_502(tmp_path):
    gh = FakeProvider("github", [], detail_error=ProviderError("github", "network error"))
    client = TestClient(create_app(make_hub(gh), tmp_path, session_token=TOKEN, shelves=[]), base_url="http://localhost")
    assert client.get("/repo/github/o/r").status_code == 502


def test_favorite_toggle_of_missing_repo_is_404(tmp_path):
    r = _not_found_setup(tmp_path).post("/favorite", data={"host": "github", "slug": "o/gone", "token": TOKEN})
    assert r.status_code == 404 and "repository not found" in r.text


class Clock:
    t = 1_000_000.0

    def __call__(self):
        return self.t


def _fav_app(tmp_path, provider_cls, clock):
    gh = provider_cls("github", [mk("github", "o/r", 5)])
    hub = make_hub(gh, clock=clock)
    hub.favorites.add(mk("github", "o/r", 5))
    clock.t += 200_000  # make the favorite stale
    app = create_app(hub, tmp_path, session_token=TOKEN, shelves=[])
    return app, hub, gh


def test_favorites_page_does_not_wait_for_the_network(tmp_path):
    import asyncio

    class Blocking(FakeProvider):
        gate = None

        async def repo(self, slug):
            await asyncio.Event().wait()  # never set

    app, hub, gh = _fav_app(tmp_path, Blocking, Clock())
    with TestClient(app, base_url="http://localhost") as client:
        r = client.get("/favorites")
        assert r.status_code == 200 and "o/r" in r.text and "background" in r.text


def test_favorites_background_failure_does_not_leak(tmp_path):
    import time

    class Boom(FakeProvider):
        async def repo(self, slug):
            raise RuntimeError("boom")

    app, hub, gh = _fav_app(tmp_path, Boom, Clock())
    with TestClient(app, base_url="http://localhost") as client:
        assert client.get("/favorites").status_code == 200
        for _ in range(100):
            if not app.state.refresh_tasks:
                break
            time.sleep(0.02)
        assert not app.state.refresh_tasks
        assert client.get("/favorites").status_code == 200


def test_favorite_toggle_uses_canonical_key(tmp_path):
    class Renamed(FakeProvider):
        async def repo(self, slug):
            return mk("github", "o/New", 5)

    hub = make_hub(Renamed("github", []))
    client = TestClient(create_app(hub, tmp_path, session_token=TOKEN, shelves=[]), base_url="http://localhost")
    data = {"host": "github", "slug": "o/old", "token": TOKEN}
    client.post("/favorite", data=data)
    assert hub.favorites.is_favorite("github:o/new")
    client.post("/favorite", data=data)
    assert not hub.favorites.is_favorite("github:o/new") and hub.favorites.list() == []


def test_unfavorite_still_works_when_provider_is_down(tmp_path):
    gh = FakeProvider("github", [mk("github", "o/r")])
    hub = make_hub(gh)
    hub.favorites.add(mk("github", "o/r"))
    gh.detail_error = ProviderError("github", "network error")
    client = TestClient(create_app(hub, tmp_path, session_token=TOKEN, shelves=[]), base_url="http://localhost")
    assert client.post("/favorite", data={"host": "github", "slug": "o/r", "token": TOKEN}).status_code == 200
    assert not hub.favorites.is_favorite("github:o/r")


def _prov(setup):
    _, hub, _ = setup
    return hub.providers["github"] if hasattr(hub, "providers") else None


@pytest.fixture
def fp(tmp_path):
    gh = FakeProvider("github", [mk("github", "o/r", 50)])
    app = create_app(make_hub(gh), tmp_path, session_token=TOKEN, shelves=[])
    return TestClient(app, base_url="http://localhost"), gh


def test_search_query_syntax_reaches_provider(fp):
    client, gh = fp
    assert client.get("/search", params={"q": "tui lang:rust stars:500 nofork sort:updated"}).status_code == 200
    f = gh.last_filters
    assert gh.last_query == "tui" and f.language == "rust" and f.min_stars == 500
    assert f.hide_forks is True and f.sort == "updated"


def test_search_form_fields_combine_and_tokens_override(fp):
    client, gh = fp
    client.get("/search", params={"q": "x", "sort": "forks", "hide_forks": "1"})
    assert gh.last_filters.sort == "forks" and gh.last_filters.hide_forks is True
    client.get("/search", params={"q": "x sort:updated", "sort": "forks"})
    assert gh.last_filters.sort == "updated"


def test_search_bad_token_shows_banner_and_keeps_query(fp):
    client, _ = fp
    r = client.get("/search", params={"q": "x stars:abc"})
    assert r.status_code == 200 and "Ignored:" in r.text and "stars needs a whole number" in r.text
    assert 'value="x stars:abc"' in r.text


def test_search_rejects_bad_sort(fp):
    client, _ = fp
    assert client.get("/search", params={"q": "x", "sort": "bogus"}).status_code == 400


def test_search_bar_shows_sort_and_hide_forks_state(fp):
    client, _ = fp
    r = client.get("/search", params={"q": "x", "sort": "forks", "hide_forks": "1"})
    assert 'name="sort"' in r.text and '<option value="forks" selected>' in r.text
    assert 'name="hide_forks" value="1" checked' in r.text
    r = client.get("/search", params={"q": "x"})
    assert '<option value="stars" selected>' in r.text and 'name="hide_forks" value="1" checked' not in r.text


def test_search_problem_banners_are_escaped_and_capped(fp):
    client, _ = fp
    r = client.get("/search", params={"q": "lang:<script>alert(1)</script> stars:[/]"})
    assert r.status_code == 200 and "<script>alert" not in r.text and "&lt;script&gt;" in r.text
    many = " ".join(f"stars:x{i}" for i in range(40))
    r = client.get("/search", params={"q": many})
    assert r.text.count("Ignored:") <= 11


# ---- curated shelves and shelf pages (Task 10) ----
from repohub.core.browse import LoadedShelves, ShelfEntry, Snapshot  # noqa: E402

XSS = '<script>alert(1)</script>'
IMG = '"><img src=x onerror=alert(2)>'


def _entries(n, note=lambda i: f"note{i}", desc=lambda i: f"snapdesc{i}"):
    return tuple(ShelfEntry("github", f"o/r{i}", note(i), Snapshot(desc(i), 100 + i, "Go", "MIT", "2026-08-01"))
                 for i in range(n))


def _curated_app(tmp_path, n=30, name="Curated one", provider=None, problems=None, extra=(), **kw):
    gh = provider or FakeProvider("github", [mk("github", f"o/r{i}", 999, description="livedesc") for i in range(n)])
    shelf = Shelf(name=name, repos=_entries(n, **kw), as_of="2026-09-01")
    if problems is None:
        app = create_app(make_hub(gh), tmp_path, session_token=TOKEN, shelves=[shelf, *extra])
    else:
        import repohub.web.app as webapp
        webapp_load = webapp.load_all_shelves
        webapp.load_all_shelves = lambda: LoadedShelves([shelf, *extra], problems)
        try:
            app = create_app(make_hub(gh), tmp_path, session_token=TOKEN)
        finally:
            webapp.load_all_shelves = webapp_load
    return TestClient(app, base_url="http://localhost"), gh


def test_home_has_see_all_links_and_curated_tiles_are_not_staggered(tmp_path):
    search = Shelf("Search one", topic="tui", min_stars=10)
    client, _ = _curated_app(tmp_path, 3, extra=(search,))
    html = client.get("/").text
    assert 'href="/shelves/0"' in html and 'href="/shelves/1"' in html
    assert 'hx-get="/shelf/0" hx-trigger="load"' in html  # curated: no delay
    assert 'hx-get="/shelf/1" hx-trigger="load delay:400ms"' in html


def test_home_stagger_is_capped(tmp_path):
    shelves = [Shelf(f"S{i}", topic="tui", min_stars=10) for i in range(13)]
    app = create_app(make_hub(FakeProvider("github")), tmp_path, session_token=TOKEN, shelves=shelves)
    html = TestClient(app, base_url="http://localhost").get("/").text
    assert 'delay:2000ms' in html and 'delay:4800ms' not in html


def test_home_shows_problem_banners_escaped_and_capped(tmp_path):
    problems = [f"bad{i} {XSS} {IMG}" for i in range(15)]
    client, _ = _curated_app(tmp_path, 2, problems=problems)
    html = client.get("/").text
    assert html.count('class="banner warn"') == 10 and "bad9" in html and "bad10" not in html
    assert XSS not in html and "<img" not in html and "&lt;script&gt;" in html


def test_home_without_problems_has_no_banners(setup):
    client, *_ = setup
    assert 'class="banner warn"' not in client.get("/").text


def test_home_tile_is_snapshot_only_with_notes_and_as_of(tmp_path):
    client, gh = _curated_app(tmp_path, 10)
    html = client.get("/shelf/0").text
    assert gh.calls == 0
    assert html.count('class="card"') == 6 and "note0" in html and "note5" in html and "note6" not in html
    assert "snapdesc0" in html and "livedesc" not in html and "as of 2026-09-01" in html
    assert 'href="/repo/github/o/r0"' in html


def test_full_page_live_has_no_marker_and_shows_notes(tmp_path):
    client, gh = _curated_app(tmp_path, 5)
    html = client.get("/shelves/0").text
    assert gh.calls == 5 and "livedesc" in html and "note3" in html and "as of" not in html
    assert "showing 1-5 of 5" in html


def test_full_page_refresh_failure_shows_snapshot_marker_and_banner(tmp_path):
    gh = FakeProvider("github", detail_error=ProviderError("github", "rate limited"))
    client, _ = _curated_app(tmp_path, 3, provider=gh)
    r = client.get("/shelves/0")
    assert r.status_code == 200
    assert "snapdesc1" in r.text and "as of 2026-09-01" in r.text
    assert "github: rate limited" in r.text and 'class="banner warn"' in r.text


def test_pagination_pages_and_links(tmp_path):
    client, _ = _curated_app(tmp_path, 30)
    p1 = client.get("/shelves/0").text
    assert "showing 1-12 of 30" in p1 and "note11" in p1 and "note12" not in p1
    assert "?page=2" in p1 and "?page=0" not in p1 and "Previous" not in p1
    p2 = client.get("/shelves/0", params={"page": "2"}).text
    assert "showing 13-24 of 30" in p2 and "note12" in p2
    assert "?page=1" in p2 and "?page=3" in p2
    p3 = client.get("/shelves/0", params={"page": "3"}).text
    assert "showing 25-30 of 30" in p3 and "?page=2" in p3 and "?page=4" not in p3 and "Next" not in p3


@pytest.mark.parametrize("bad", ["0", "-1", "4", "999", "abc", "1.5", "99999999999999999999999"])
def test_bad_page_is_404_html_not_500(tmp_path, bad):
    client, _ = _curated_app(tmp_path, 30)
    r = client.get("/shelves/0", params={"page": bad})
    assert r.status_code == 404 and 'class="banner"' in r.text


def test_empty_page_param_means_first_page_and_bad_index_404(tmp_path):
    client, _ = _curated_app(tmp_path, 30)
    assert client.get("/shelves/0", params={"page": ""}).status_code == 200
    assert client.get("/shelves/1").status_code == 404
    assert client.get("/shelves/-1").status_code == 404


def test_search_shelf_full_page_lists_results(setup):
    client, *_ = setup
    r = client.get("/shelves/0")
    assert r.status_code == 200 and "Terminal tools" in r.text and "o/r" in r.text
    assert "<script>alert(1)</script>" not in r.text
    assert client.get("/shelves/0", params={"page": "2"}).status_code == 404  # a search shelf has one page


def test_curated_untrusted_text_is_escaped_everywhere(tmp_path):
    client, _ = _curated_app(tmp_path, 3, name=f"Name {XSS}{IMG}",
                             provider=FakeProvider("github", detail_error=ProviderError("github", f"err {XSS}")),
                             note=lambda i: f"n {XSS}{IMG}", desc=lambda i: f"d {XSS}{IMG}")
    for url in ("/", "/shelf/0", "/shelves/0"):
        html = client.get(url).text
        assert "<script>" not in html and "<img" not in html
    assert "&lt;script&gt;" in client.get("/shelves/0").text
    assert "&lt;script&gt;" in client.get("/").text


def test_curated_links_are_built_from_validated_entries(tmp_path):
    client, _ = _curated_app(tmp_path, 2)
    html = client.get("/shelves/0").text
    assert 'href="/repo/github/o/r0"' in html and 'href="/repo/github/o/r1"' in html


@pytest.mark.parametrize("value,expected", [
    ("", False), ("0", False), ("false", False), ("OFF", False), ("No", False),
    ("1", True), ("on", True), ("true", True), ("yes", True)])
def test_checkbox_values(fp, value, expected):
    client, gh = fp
    r = client.get("/search", params={"q": "x", "hide_forks": value, "archived": value})
    assert r.status_code == 200
    assert gh.last_filters.hide_forks is expected and gh.last_filters.include_archived is expected
    checked = 'name="hide_forks" value="1" checked' in r.text
    assert checked is expected


# ---- Task 6: every registered host ----
from repohub.core.hosts import BUILTIN_HOSTS, HostRegistry, HostSpec, set_registry  # noqa: E402


def _multi(tmp_path, extra_host=None, **hubkw):
    gh = FakeProvider("github", [mk("github", "o/r", 50)])
    gl = FakeProvider("gitlab", [mk("gitlab", "g/sub/p", 40)])
    cb = FakeProvider("codeberg", [mk("codeberg", "o/r", 30, url="https://codeberg.org/o/r")])
    provs = [gh, gl, cb]
    if extra_host:
        provs.append(FakeProvider(extra_host, [mk(extra_host, "o/x", 5)]))
    hub = make_hub(*provs)
    hub.host_problems = hubkw.get("host_problems", [])
    calls = []

    def cloner(url, root):
        calls.append((url, root))
        return tmp_path / "r"

    app = create_app(hub, tmp_path, session_token=TOKEN, shelves=[], cloner=cloner)
    return TestClient(app, base_url="http://localhost"), hub, provs, calls


def test_search_host_codeberg_reaches_only_codeberg(tmp_path):
    client, _, (gh, gl, cb), _ = _multi(tmp_path)
    assert client.get("/search", params={"q": "x", "host": "codeberg"}).status_code == 200
    assert cb.calls == 1 and gh.calls == 0 and gl.calls == 0


def test_search_unknown_host_is_400(tmp_path):
    client, *_ = _multi(tmp_path)
    assert client.get("/search", params={"q": "x", "host": "nowhere"}).status_code == 400


@pytest.mark.parametrize("value", ["all", "both"])
def test_search_all_and_legacy_both_query_every_provider(tmp_path, value):
    client, _, provs, _ = _multi(tmp_path)
    r = client.get("/search", params={"q": "x", "host": value})
    assert r.status_code == 200 and all(p.calls == 1 for p in provs)
    assert '<option value="all" selected>' in r.text


def test_dropdown_lists_all_registered_hosts(tmp_path):
    set_registry(HostRegistry(BUILTIN_HOSTS + (HostSpec("mine", "forgejo", "Mine", "git.example.org",
                                                        "https://git.example.org/api/v1"),)))
    client, *_ = _multi(tmp_path, extra_host="mine")
    html = client.get("/").text
    for h in ("all", "github", "gitlab", "codeberg", "mine"):
        assert f'<option value="{h}"' in html
    assert '<option value="both"' not in html
    assert "Search GitHub and GitLab" not in html


def test_repo_page_for_codeberg_has_badge(tmp_path):
    client, *_ = _multi(tmp_path)
    r = client.get("/repo/codeberg/o/r")
    assert r.status_code == 200 and 'class="badge host-codeberg"' in r.text


def test_repo_page_rejects_bad_codeberg_slug_and_unknown_host(tmp_path):
    client, *_ = _multi(tmp_path)
    assert client.get("/repo/codeberg/o/r/readme").status_code == 404
    assert client.get("/repo/nowhere/o/r").status_code == 404
    assert client.get("/repo/gitlab/g/sub/p").status_code == 200


def test_favorite_whose_host_is_gone_renders_and_links_404(tmp_path):
    client, hub, *_ = _multi(tmp_path)
    hub.favorites.add(mk("oldhost", "o/r", 5, description=XSS))
    r = client.get("/favorites")
    assert r.status_code == 200 and "o/r" in r.text and XSS not in r.text
    assert client.get("/repo/oldhost/o/r").status_code == 404
    assert client.get("/clone", params={"host": "oldhost", "slug": "o/r"}).status_code == 404


def test_favorite_toggle_for_codeberg(tmp_path):
    client, hub, *_ = _multi(tmp_path)
    r = client.post("/favorite", data={"host": "codeberg", "slug": "o/r", "token": TOKEN})
    assert r.status_code == 200 and "Favorited" in r.text and hub.favorites.is_favorite("codeberg:o/r")
    assert client.post("/favorite", data={"host": "codeberg", "slug": "o/r", "token": "bad"}).status_code == 403
    r = client.post("/favorite", data={"host": "codeberg", "slug": "o/r", "token": TOKEN})
    assert "Favorited" not in r.text and not hub.favorites.is_favorite("codeberg:o/r")
    assert client.post("/favorite", data={"host": "nowhere", "slug": "o/r", "token": TOKEN}).status_code == 404


def test_clone_confirm_and_post_for_codeberg(tmp_path):
    client, _, _, calls = _multi(tmp_path)
    r = client.get("/clone", params={"host": "codeberg", "slug": "o/r"})
    assert r.status_code == 200 and "Clone o/r?" in r.text and "<pre>" in r.text
    assert 'name="host" value="codeberg"' in r.text
    assert client.get("/clone", params={"host": "codeberg", "slug": "o/r/x"}).status_code == 404
    assert client.post("/clone", data={"host": "codeberg", "slug": "o/r", "token": "bad"}).status_code == 403
    assert not calls
    r = client.post("/clone", data={"host": "codeberg", "slug": "o/r", "token": TOKEN})
    assert r.status_code == 200 and calls == [("https://codeberg.org/o/r.git", tmp_path)]
    assert "Cloned to" in r.text


def test_host_problem_banners_are_escaped_and_capped(tmp_path):
    problems = [f"hp{i} {XSS} {IMG}" for i in range(15)]
    client, *_ = _multi(tmp_path, host_problems=problems)
    html = client.get("/").text
    assert html.count('class="banner warn"') == 10 and "hp9" in html and "hp10" not in html
    assert XSS not in html and "<img" not in html and "&lt;script&gt;" in html and "&lt;img" in html


def test_no_host_problems_no_banners(tmp_path):
    client, *_ = _multi(tmp_path)
    assert 'class="banner warn"' not in client.get("/").text


def test_css_has_codeberg_badge(tmp_path):
    client, *_ = _multi(tmp_path)
    assert ".badge.host-codeberg" in client.get("/static/app.css").text


def test_unconfigured_host_shelf_renders_snapshot_and_error_banner(tmp_path):
    gh = FakeProvider("github")
    shelf = Shelf(name="Gone", repos=(ShelfEntry("gone", "o/r", "gnote", Snapshot("gsnap", 5, "Go", "MIT", "2026-08-01")),),
                  as_of="2026-09-01")
    client = TestClient(create_app(make_hub(gh), tmp_path, session_token=TOKEN, shelves=[shelf]),
                        base_url="http://localhost")
    html = client.get("/shelves/0").text
    assert "gsnap" in html and "gnote" in html
    assert '<p class="banner warn">gone: host not configured</p>' in html
    assert gh.calls == 0


def test_codeberg_shelf_on_home_page(tmp_path, monkeypatch):
    monkeypatch.setattr("repohub.core.browse.personal_shelves_path", lambda: tmp_path / "none.yaml")
    client = TestClient(create_app(make_hub(FakeProvider("github")), tmp_path, session_token=TOKEN),
                        base_url="http://localhost")
    assert "Catalog: Codeberg" in client.get("/").text


def test_badge_class_is_namespaced_so_an_id_cannot_collide_with_ok(tmp_path):
    from repohub.core.hosts import BUILTIN_HOSTS, HostRegistry, HostSpec, set_registry
    set_registry(HostRegistry(BUILTIN_HOSTS + (HostSpec("ok", "forgejo", "ok", "ok.example.org",
                                                        "https://ok.example.org/api/v1"),)))
    from helpers import FakeProvider, make_hub, mk
    from fastapi.testclient import TestClient
    from repohub.web.app import create_app
    hub = make_hub(FakeProvider("ok", [mk("ok", "o/r")]))
    client = TestClient(create_app(hub, tmp_path, shelves=[]), base_url="http://localhost")
    html = client.get("/search", params={"q": "x"}).text
    assert 'class="badge host-ok"' in html and 'class="badge ok"' not in html
    assert ".badge.host-codeberg" in client.get("/static/app.css").text


# ---- favorites of hosts that are no longer configured -----------------------------------------------

def _gone_setup(tmp_path):
    hub = make_hub(FakeProvider("github", [mk("github", "o/r", 5)]))
    hub.favorites.add(mk("oldforge", "o/gone", 7, description="snap desc"))
    app = create_app(hub, tmp_path, session_token=TOKEN, shelves=[])
    return TestClient(app, base_url="http://localhost"), hub


def test_favorites_page_shows_unconfigured_host_as_unavailable_with_remove(tmp_path):
    client, hub = _gone_setup(tmp_path)
    r = client.get("/favorites")
    assert r.status_code == 200
    assert "host not configured" in r.text and "o/gone" in r.text and "snap desc" in r.text
    assert 'href="/repo/oldforge/' not in r.text
    assert 'hx-post="/favorite"' in r.text and "Remove" in r.text and f'value="{TOKEN}"' in r.text


def test_removing_an_unconfigured_favorite_makes_no_provider_call(tmp_path):
    client, hub = _gone_setup(tmp_path)
    called = []
    hub.detail = lambda *a, **k: called.append(a)
    r = client.post("/favorite", data={"host": "oldforge", "slug": "o/gone", "token": TOKEN})
    assert r.status_code == 200 and not hub.favorites.is_favorite("oldforge:o/gone") and called == []


def test_unconfigured_host_favorite_can_never_be_added(tmp_path):
    client, hub = _gone_setup(tmp_path)
    r = client.post("/favorite", data={"host": "oldforge", "slug": "o/other", "token": TOKEN})
    assert r.status_code == 404 and not hub.favorites.is_favorite("oldforge:o/other")
    r = client.post("/favorite", data={"host": "nowhere", "slug": "o/x", "token": TOKEN})
    assert r.status_code == 404


def test_removing_an_unconfigured_favorite_still_needs_the_token(tmp_path):
    client, hub = _gone_setup(tmp_path)
    assert client.post("/favorite", data={"host": "oldforge", "slug": "o/gone"}).status_code == 403
    assert client.post("/favorite", data={"host": "oldforge", "slug": "o/gone", "token": "bad"}).status_code == 403
    assert hub.favorites.is_favorite("oldforge:o/gone")
