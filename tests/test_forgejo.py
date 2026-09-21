import httpx
import pytest
import respx

from repohub.core.models import SearchFilters
from repohub.core.providers.base import NotFound, ProviderError, RateLimited
from repohub.core.providers.forgejo import ForgejoProvider

API = "https://codeberg.org/api/v1"
ITEM = {
    "id": 101, "owner": {"id": 5, "login": "o", "is_admin": False}, "name": "r", "full_name": "o/r",
    "description": "A tool", "empty": False, "private": False, "fork": False, "template": False, "parent": None,
    "mirror": False, "size": 1234, "language": "Rust", "languages_url": "https://codeberg.org/api/v1/repos/o/r/languages",
    "html_url": "https://codeberg.org/o/r", "ssh_url": "git@codeberg.org:o/r.git", "clone_url": "https://codeberg.org/o/r.git",
    "website": "https://r.example.org", "stars_count": 42, "forks_count": 3, "watchers_count": 7,
    "open_issues_count": 1, "default_branch": "main", "archived": False, "created_at": "2025-01-01T00:00:00+01:00",
    "updated_at": "2026-09-18T13:02:54+02:00", "topics": ["cli", "Tools"], "internal_tracker": {"x": 1},
}


def search_resp(*items):
    return httpx.Response(200, json={"ok": True, "data": list(items)})


@respx.mock
async def test_search_params_and_mapping():
    route = respx.get(f"{API}/repos/search").mock(return_value=search_resp(ITEM))
    repos = await ForgejoProvider("codeberg", API).search("cli", SearchFilters())
    p = route.calls.last.request.url.params
    assert p["q"] == "cli" and p["sort"] == "stars" and p["order"] == "desc" and p["limit"] == "30"
    assert p["archived"] == "false" and "mode" not in p and "topic" not in p
    assert "language" not in p and "min_stars" not in p
    r = repos[0]
    assert (r.host, r.slug, r.url, r.description) == ("codeberg", "o/r", "https://codeberg.org/o/r", "A tool")
    assert (r.stars, r.forks, r.language, r.license, r.archived, r.fork) == (42, 3, "Rust", "", False, False)
    assert r.topics == ("cli", "Tools") and r.homepage == "https://r.example.org"
    assert r.pushed_at == "2026-09-18T11:02:54Z"


@respx.mock
async def test_limit_capped_at_50():
    route = respx.get(f"{API}/repos/search").mock(return_value=search_resp())
    await ForgejoProvider("codeberg", API).search("x", SearchFilters(), per_page=200)
    assert route.calls.last.request.url.params["limit"] == "50"


@respx.mock
async def test_mode_source_only_with_hide_forks():
    route = respx.get(f"{API}/repos/search").mock(return_value=search_resp())
    p = ForgejoProvider("codeberg", API)
    await p.search("x", SearchFilters(hide_forks=True))
    assert route.calls.last.request.url.params["mode"] == "source"
    await p.search("x", SearchFilters(hide_forks=False))
    assert "mode" not in route.calls.last.request.url.params


@respx.mock
async def test_archived_default_and_included():
    route = respx.get(f"{API}/repos/search").mock(return_value=search_resp())
    p = ForgejoProvider("codeberg", API)
    await p.search("x", SearchFilters())
    assert route.calls.last.request.url.params["archived"] == "false"
    await p.search("x", SearchFilters(include_archived=True))
    assert "archived" not in route.calls.last.request.url.params


@pytest.mark.parametrize("sort,sent", [("stars", "stars"), ("forks", "stars"), ("updated", "updated"), ("bogus", "stars")])
@respx.mock
async def test_sort_mapping(sort, sent):
    route = respx.get(f"{API}/repos/search").mock(return_value=search_resp())
    await ForgejoProvider("codeberg", API).search("x", SearchFilters(sort=sort))
    assert route.calls.last.request.url.params["sort"] == sent


@respx.mock
async def test_topic_only_uses_topic_flag():
    route = respx.get(f"{API}/repos/search").mock(return_value=search_resp(ITEM))
    repos = await ForgejoProvider("codeberg", API).search("", SearchFilters(topic="cli"))
    p = route.calls.last.request.url.params
    assert p["q"] == "cli" and p["topic"] == "true"
    assert len(repos) == 1


@respx.mock
async def test_topic_with_text_filters_client_side():
    other = dict(ITEM, full_name="o/other", topics=["web"])
    notopics = {k: v for k, v in ITEM.items() if k != "topics"} | {"full_name": "o/none"}
    route = respx.get(f"{API}/repos/search").mock(return_value=search_resp(ITEM, other, notopics))
    repos = await ForgejoProvider("codeberg", API).search("tool", SearchFilters(topic="CLI"))
    p = route.calls.last.request.url.params
    assert p["q"] == "tool" and "topic" not in p
    assert [r.slug for r in repos] == ["o/r"]


@respx.mock
async def test_language_filter_case_insensitive_and_not_sent():
    other = dict(ITEM, full_name="o/py", language="Python")
    nolang = dict(ITEM, full_name="o/nl", language=None)
    route = respx.get(f"{API}/repos/search").mock(return_value=search_resp(ITEM, other, nolang))
    repos = await ForgejoProvider("codeberg", API).search("x", SearchFilters(language="rUSt"))
    assert [r.slug for r in repos] == ["o/r"]
    assert "language" not in route.calls.last.request.url.params


@respx.mock
async def test_stars_and_recency_not_filtered_here():
    low = dict(ITEM, full_name="o/low", stars_count=0, updated_at="2001-01-01T00:00:00Z")
    route = respx.get(f"{API}/repos/search").mock(return_value=search_resp(low))
    repos = await ForgejoProvider("codeberg", API).search("x", SearchFilters(min_stars=100, pushed_after="2026-01-01"))
    assert [r.slug for r in repos] == ["o/low"]
    assert "stars" not in str(route.calls.last.request.url.params).replace("sort=stars", "")


@pytest.mark.parametrize("value,expected", [
    ("2026-09-18T13:02:54+02:00", "2026-09-18T11:02:54Z"),
    ("2026-09-18T13:02:54Z", "2026-09-18T13:02:54Z"),
    ("2026-09-18T13:02:54-05:00", "2026-09-18T18:02:54Z"),
    ("2026-09-18T13:02:54", "2026-09-18T13:02:54Z"),
    ("garbage", ""), ("", ""), (None, ""),
])
@respx.mock
async def test_updated_at_normalised(value, expected):
    respx.get(f"{API}/repos/search").mock(return_value=search_resp(dict(ITEM, updated_at=value)))
    repos = await ForgejoProvider("codeberg", API).search("x", SearchFilters())
    assert repos[0].pushed_at == expected


@respx.mock
async def test_custom_base_url_and_host_id():
    base = "https://git.example.org/api/v1"
    item = {k: v for k, v in ITEM.items() if k != "html_url"}
    route = respx.get(f"{base}/repos/search").mock(return_value=search_resp(item))
    repos = await ForgejoProvider("myforge", base).search("x", SearchFilters())
    assert route.called
    assert repos[0].host == "myforge" and repos[0].url == "https://git.example.org/o/r"


@pytest.mark.parametrize("resp", [
    httpx.Response(200, json={"ok": True}),
    httpx.Response(200, text="<html>nope</html>"),
    httpx.Response(200, json=[ITEM]),
    httpx.Response(200, json={"ok": True, "data": "abc"}),
    httpx.Response(200, json={"ok": True, "data": None}),
    httpx.Response(200, json={"ok": True, "data": {"a": 1}}),
    httpx.Response(200, json={"ok": True, "data": [{"description": "no name"}]}),
    httpx.Response(200, json={"ok": True, "data": [ITEM | {"stars_count": "many"}]}),
    httpx.Response(404),
])
@respx.mock
async def test_malformed_search_is_provider_error(resp):
    respx.get(f"{API}/repos/search").mock(return_value=resp)
    with pytest.raises(ProviderError):
        await ForgejoProvider("codeberg", API).search("x", SearchFilters())


@respx.mock
async def test_rate_limit_with_retry_after(monkeypatch):
    monkeypatch.setattr("repohub.core.providers.forgejo.time.time", lambda: 1000)
    respx.get(f"{API}/repos/search").mock(return_value=httpx.Response(429, headers={"retry-after": "30"}))
    with pytest.raises(RateLimited) as ei:
        await ForgejoProvider("codeberg", API).search("x", SearchFilters())
    assert ei.value.reset_at == 1030 and ei.value.host == "codeberg"
    respx.get(f"{API}/repos/search").mock(return_value=httpx.Response(429))
    with pytest.raises(RateLimited) as ei:
        await ForgejoProvider("codeberg", API).search("x", SearchFilters())
    assert ei.value.reset_at is None


@respx.mock
async def test_rejected_token_falls_back_to_anonymous_once():
    route = respx.get(f"{API}/repos/search").mock(side_effect=[httpx.Response(401), search_resp(ITEM), search_resp(ITEM)])
    p = ForgejoProvider("codeberg", API, token="bad")
    repos = await p.search("x", SearchFilters())
    assert len(repos) == 1 and p.token_rejected is True
    assert route.calls[0].request.headers["authorization"] == "token bad"
    assert "authorization" not in route.calls[1].request.headers
    await p.search("x", SearchFilters())
    assert "authorization" not in route.calls.last.request.headers


@respx.mock
async def test_second_401_raises_and_does_not_loop():
    route = respx.get(f"{API}/repos/search").mock(return_value=httpx.Response(401))
    with pytest.raises(ProviderError, match="token rejected") as ei:
        await ForgejoProvider("codeberg", API, token="bad").search("x", SearchFilters())
    assert route.call_count == 2 and "bad" not in str(ei.value)
    route.reset()
    with pytest.raises(ProviderError, match="token rejected"):
        await ForgejoProvider("codeberg", API).search("x", SearchFilters())
    assert route.call_count == 1


@respx.mock
async def test_redirect_is_error_not_followed():
    respx.get(f"{API}/repos/search").mock(return_value=httpx.Response(302, headers={"location": "https://evil.example/"}))
    evil = respx.get("https://evil.example/").mock(return_value=httpx.Response(200, json={"ok": True, "data": []}))
    with pytest.raises(ProviderError, match="302"):
        await ForgejoProvider("codeberg", API, token="tok").search("x", SearchFilters())
    assert not evil.called


@respx.mock
async def test_server_error_is_provider_error():
    respx.get(f"{API}/repos/search").mock(return_value=httpx.Response(500))
    with pytest.raises(ProviderError, match="500"):
        await ForgejoProvider("codeberg", API).search("x", SearchFilters())


@respx.mock
async def test_network_error():
    respx.get(f"{API}/repos/search").mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(ProviderError, match="network error"):
        await ForgejoProvider("codeberg", API).search("x", SearchFilters())


@respx.mock
async def test_token_header_only_when_set():
    route = respx.get(f"{API}/repos/search").mock(return_value=search_resp())
    await ForgejoProvider("codeberg", API, token="tok").search("x", SearchFilters())
    h = route.calls.last.request.headers
    assert h["authorization"] == "token tok" and h["user-agent"] == "repohub"
    await ForgejoProvider("codeberg", API).search("x", SearchFilters())
    assert "authorization" not in route.calls.last.request.headers


@respx.mock
async def test_token_never_sent_to_other_host():
    respx.get(f"{API}/repos/search").mock(return_value=search_resp())
    other = respx.get("https://git.example.org/api/v1/repos/search").mock(return_value=search_resp())
    await ForgejoProvider("codeberg", API, token="tok").search("x", SearchFilters())
    assert not other.called
    await ForgejoProvider("myforge", "https://git.example.org/api/v1").search("x", SearchFilters())
    assert "authorization" not in other.calls.last.request.headers


@pytest.mark.parametrize("slug", ["../x", "o/r/x", "o/r\n", "o", "", "o/..", "./r", "o/r?x=1", "o/r#f", "o//r", "/o/r"])
@respx.mock
async def test_invalid_slug_never_reaches_network(slug):
    route = respx.route().mock(return_value=httpx.Response(200, json={}))
    p = ForgejoProvider("codeberg", API)
    for call in (p.repo, p.readme, p.latest_release):
        with pytest.raises(ProviderError, match="invalid repository name"):
            await call(slug)
    assert not route.called


@respx.mock
async def test_repo_and_not_found():
    route = respx.get(f"{API}/repos/o/r").mock(return_value=httpx.Response(200, json=ITEM))
    r = await ForgejoProvider("codeberg", API).repo("o/r")
    assert route.called and r.slug == "o/r" and r.stars == 42
    respx.get(f"{API}/repos/o/gone").mock(return_value=httpx.Response(404))
    with pytest.raises(NotFound):
        await ForgejoProvider("codeberg", API).repo("o/gone")


@respx.mock
async def test_readme_candidate_order_and_404_fall_through():
    names = ["README.md", "README.markdown", "README.rst", "README.txt", "README"]
    routes = {n: respx.get(f"{API}/repos/o/r/raw/{n}").mock(return_value=httpx.Response(404)) for n in names}
    routes["README.rst"].mock(return_value=httpx.Response(200, text="hello\x1b[31m world"))
    text = await ForgejoProvider("codeberg", API).readme("o/r")
    assert text.startswith("hello") and "\x1b" not in text
    assert routes["README.md"].called and routes["README.markdown"].called and routes["README.rst"].called
    assert not routes["README.txt"].called and not routes["README"].called


@respx.mock
async def test_readme_order_recorded():
    for n in ("README.md", "README.markdown", "README.rst", "README.txt", "README"):
        respx.get(f"{API}/repos/o/r/raw/{n}").mock(return_value=httpx.Response(404))
    assert await ForgejoProvider("codeberg", API).readme("o/r") is None
    assert [c.request.url.path.rsplit("/", 1)[1] for c in respx.calls] == [
        "README.md", "README.markdown", "README.rst", "README.txt", "README"]


@respx.mock
async def test_readme_other_error_propagates():
    respx.get(f"{API}/repos/o/r/raw/README.md").mock(return_value=httpx.Response(500))
    later = respx.get(f"{API}/repos/o/r/raw/README.markdown").mock(return_value=httpx.Response(200, text="x"))
    with pytest.raises(ProviderError, match="500"):
        await ForgejoProvider("codeberg", API).readme("o/r")
    assert not later.called


@respx.mock
async def test_release_mapping_with_arm64():
    rel = {"id": 1, "tag_name": "v1.2", "name": "1.2", "draft": False, "published_at": "2026-09-01T10:00:00Z", "assets": [
        {"id": 1, "name": "tool-linux-arm64.tar.gz", "size": 100, "download_count": 5, "uuid": "u1", "type": "attachment",
         "browser_download_url": "https://codeberg.org/attachments/u1"},
        {"id": 2, "name": "tool-linux-x86_64.tar.gz", "size": 200, "uuid": "u2", "type": "attachment",
         "browser_download_url": "https://codeberg.org/attachments/u2"},
        {"id": 3, "name": "evil.bin", "size": 1, "browser_download_url": "javascript:alert(1)"},
        {"id": 4, "name": "nourl.bin", "size": 1},
    ]}
    route = respx.get(f"{API}/repos/o/r/releases/latest").mock(return_value=httpx.Response(200, json=rel))
    r = await ForgejoProvider("codeberg", API).latest_release("o/r")
    assert route.called and r.tag == "v1.2" and r.published_at == "2026-09-01T10:00:00Z"
    assert [(a.name, a.size, a.url, a.arch) for a in r.assets] == [
        ("tool-linux-arm64.tar.gz", 100, "https://codeberg.org/attachments/u1", "arm64"),
        ("tool-linux-x86_64.tar.gz", 200, "https://codeberg.org/attachments/u2", "x86_64")]
    assert r.has_arm64


@respx.mock
async def test_no_release_is_none_and_no_assets_ok():
    respx.get(f"{API}/repos/o/r/releases/latest").mock(return_value=httpx.Response(404))
    assert await ForgejoProvider("codeberg", API).latest_release("o/r") is None
    respx.get(f"{API}/repos/o/r/releases/latest").mock(
        return_value=httpx.Response(200, json={"tag_name": "v1", "published_at": None}))
    r = await ForgejoProvider("codeberg", API).latest_release("o/r")
    assert r.tag == "v1" and r.assets == ()


@respx.mock
async def test_release_missing_tag_is_provider_error():
    respx.get(f"{API}/repos/o/r/releases/latest").mock(return_value=httpx.Response(200, json={"assets": []}))
    with pytest.raises(ProviderError):
        await ForgejoProvider("codeberg", API).latest_release("o/r")


@respx.mock
async def test_hostile_text_cleaned():
    bad = dict(ITEM, description="hi\x00\x1b[31m there‮", topics=["a\x07b", "ok"], language="Ru\x00st",
               full_name="o/r")
    respx.get(f"{API}/repos/search").mock(return_value=search_resp(bad))
    r = (await ForgejoProvider("codeberg", API).search("x", SearchFilters()))[0]
    for text in (r.description, r.language, *r.topics):
        assert not any(ord(c) < 32 or ord(c) == 127 or c == "‮" for c in text)


@respx.mock
async def test_release_text_cleaned():
    respx.get(f"{API}/repos/o/r/releases/latest").mock(return_value=httpx.Response(200, json={
        "tag_name": "v1\x00\x1b[31m", "assets": [{"name": "a\x00b.tgz", "size": 1, "browser_download_url": "https://x.example/a"}]}))
    r = await ForgejoProvider("codeberg", API).latest_release("o/r")
    assert "\x00" not in r.tag and "\x1b" not in r.tag and "\x00" not in r.assets[0].name


@respx.mock
async def test_javascript_urls_neutralised():
    bad = dict(ITEM, website="javascript:alert(1)", html_url="javascript:alert(2)")
    respx.get(f"{API}/repos/search").mock(return_value=search_resp(bad))
    r = (await ForgejoProvider("codeberg", API).search("x", SearchFilters()))[0]
    assert r.homepage == "" and r.url == "https://codeberg.org/o/r"


@respx.mock
async def test_missing_optional_fields_tolerated():
    minimal = {"full_name": "o/m", "html_url": "https://codeberg.org/o/m", "stars_count": 1}
    respx.get(f"{API}/repos/search").mock(return_value=search_resp(minimal))
    r = (await ForgejoProvider("codeberg", API).search("x", SearchFilters()))[0]
    assert (r.description, r.language, r.topics, r.homepage, r.pushed_at, r.forks) == ("", "", (), "", "", 0)
    assert r.archived is False and r.fork is False
    nulls = dict(minimal, description=None, language=None, topics=None, website=None, updated_at=None)
    respx.get(f"{API}/repos/search").mock(return_value=search_resp(nulls))
    r = (await ForgejoProvider("codeberg", API).search("x", SearchFilters()))[0]
    assert r.topics == () and r.homepage == ""


@respx.mock
async def test_fork_and_archived_flags_map():
    respx.get(f"{API}/repos/search").mock(return_value=search_resp(dict(ITEM, fork=True, archived=True)))
    r = (await ForgejoProvider("codeberg", API).search("x", SearchFilters(include_archived=True)))[0]
    assert r.fork is True and r.archived is True


async def test_aclose():
    p = ForgejoProvider("codeberg", API)
    await p.aclose()


# ---- hardening against hostile configured hosts ----
from repohub.core.models import MAX_STARS  # noqa: E402

DEEP = b"[" * 100000 + b"]" * 100000


def _raw(body: bytes):
    return httpx.Response(200, content=body, headers={"content-type": "application/json"})


@pytest.mark.parametrize("body", [
    b'{"ok": true, "data": [{"full_name": "o/r", "stars_count": Infinity}]}',
    b'{"ok": true, "data": [{"full_name": "o/r", "stars_count": NaN}]}',
    b'{"ok": true, "data": ' + DEEP + b'}',
])
@respx.mock
async def test_hostile_json_search_is_provider_error(body):
    respx.get(f"{API}/repos/search").mock(return_value=_raw(body))
    with pytest.raises(ProviderError):
        await ForgejoProvider("codeberg", API).search("x", SearchFilters())


@pytest.mark.parametrize("body", [
    b'{"full_name": "o/r", "stars_count": Infinity}',
    DEEP,
])
@respx.mock
async def test_hostile_json_repo_and_release_are_provider_errors(body):
    respx.get(f"{API}/repos/o/r").mock(return_value=_raw(body))
    respx.get(f"{API}/repos/o/r/releases/latest").mock(return_value=_raw(body))
    with pytest.raises(ProviderError):
        await ForgejoProvider("codeberg", API).repo("o/r")
    if body is DEEP:
        with pytest.raises(ProviderError):
            await ForgejoProvider("codeberg", API).latest_release("o/r")


@respx.mock
async def test_release_size_infinity_is_not_a_raw_exception():
    respx.get(f"{API}/repos/o/r/releases/latest").mock(return_value=_raw(
        b'{"tag_name": "v1", "assets": [{"name": "a", "size": Infinity, "browser_download_url": "https://x.example/a"}]}'))
    r = await ForgejoProvider("codeberg", API).latest_release("o/r")
    assert r.assets[0].size == 0


@pytest.mark.parametrize("value", ["9999-12-31T23:59:59-05:00", "0001-01-01T00:00:00+05:00"])
@respx.mock
async def test_out_of_range_date_becomes_empty(value):
    respx.get(f"{API}/repos/search").mock(return_value=search_resp(dict(ITEM, updated_at=value)))
    respx.get(f"{API}/repos/o/r").mock(return_value=httpx.Response(200, json=dict(ITEM, updated_at=value)))
    assert (await ForgejoProvider("codeberg", API).search("x", SearchFilters()))[0].pushed_at == ""
    assert (await ForgejoProvider("codeberg", API).repo("o/r")).pushed_at == ""


BAD_SLUGS = ["../../etc/passwd", "a/b/c", "a b/c", "o/..", "o/r?x=1", "ö/ü", "", "o/" + "r" * 5000, "o", "o/r#f"]


@respx.mock
async def test_search_drops_invalid_slugs_keeps_valid():
    items = [dict(ITEM, full_name=s) for s in BAD_SLUGS] + [dict(ITEM, full_name="o/good"), dict(ITEM, full_name="o/r\n")]
    respx.get(f"{API}/repos/search").mock(return_value=search_resp(*items))
    repos = await ForgejoProvider("codeberg", API).search("x", SearchFilters())
    assert [r.slug for r in repos] == ["o/good", "o/r"]


@pytest.mark.parametrize("slug", BAD_SLUGS)
@respx.mock
async def test_repo_rejects_invalid_returned_slug(slug):
    respx.get(f"{API}/repos/o/r").mock(return_value=httpx.Response(200, json=dict(ITEM, full_name=slug)))
    with pytest.raises(ProviderError, match="unexpected response"):
        await ForgejoProvider("codeberg", API).repo("o/r")


@respx.mock
async def test_repo_cleans_trailing_newline_in_slug():
    respx.get(f"{API}/repos/o/r").mock(return_value=httpx.Response(200, json=dict(ITEM, full_name="o/r\n")))
    assert (await ForgejoProvider("codeberg", API).repo("o/r")).slug == "o/r"


@respx.mock
async def test_fallback_url_uses_cleaned_slug():
    item = dict(ITEM, full_name="o/r\n", html_url="javascript:x")
    respx.get(f"{API}/repos/search").mock(return_value=search_resp(item))
    r = (await ForgejoProvider("codeberg", API).search("x", SearchFilters()))[0]
    assert r.url == "https://codeberg.org/o/r"


@pytest.mark.parametrize("stars,expected", [
    (10**30, MAX_STARS), (-5, 0), (7.0, 7), (0, 0), (MAX_STARS, MAX_STARS),
])
@respx.mock
async def test_stars_clamped_or_accepted(stars, expected):
    respx.get(f"{API}/repos/search").mock(return_value=search_resp(dict(ITEM, stars_count=stars)))
    assert (await ForgejoProvider("codeberg", API).search("x", SearchFilters()))[0].stars == expected


@pytest.mark.parametrize("stars", [True, False, "5", None, 1.5, [1], {"a": 1}])
@respx.mock
async def test_invalid_stars_is_provider_error(stars):
    respx.get(f"{API}/repos/search").mock(return_value=search_resp(dict(ITEM, stars_count=stars)))
    with pytest.raises(ProviderError):
        await ForgejoProvider("codeberg", API).search("x", SearchFilters())


@pytest.mark.parametrize("forks,expected", [
    (10**30, MAX_STARS), (-1, 0), (4.0, 4), (True, 0), ("3", 0), (None, 0), (1.5, 0), ([], 0)])
@respx.mock
async def test_forks_clamped_or_zero(forks, expected):
    respx.get(f"{API}/repos/search").mock(return_value=search_resp(dict(ITEM, forks_count=forks)))
    assert (await ForgejoProvider("codeberg", API).search("x", SearchFilters()))[0].forks == expected


@respx.mock
async def test_forks_infinity_is_zero():
    respx.get(f"{API}/repos/search").mock(return_value=_raw(
        b'{"ok": true, "data": [{"full_name": "o/r", "stars_count": 1, "forks_count": Infinity}]}'))
    assert (await ForgejoProvider("codeberg", API).search("x", SearchFilters()))[0].forks == 0


@respx.mock
async def test_release_field_types():
    respx.get(f"{API}/repos/o/r/releases/latest").mock(return_value=httpx.Response(200, json={
        "tag_name": "v1", "published_at": 12345, "assets": [
            {"name": "a", "size": -3, "browser_download_url": "https://x.example/a"},
            {"name": "b", "size": "9", "browser_download_url": "https://x.example/b"},
            {"name": "c", "size": True, "browser_download_url": "https://x.example/c"},
            {"name": "d", "size": 12, "browser_download_url": "https://x.example/d"}]}))
    r = await ForgejoProvider("codeberg", API).latest_release("o/r")
    assert r.published_at is None and [a.size for a in r.assets] == [0, 0, 0, 12]
    respx.get(f"{API}/repos/o/r/releases/latest").mock(return_value=httpx.Response(200, json={
        "tag_name": "v1", "published_at": "2026-09-01\x00T10:00:00Z"}))
    assert "\x00" not in (await ForgejoProvider("codeberg", API).latest_release("o/r")).published_at


@respx.mock
async def test_casefold_filters():
    a = dict(ITEM, full_name="o/a", language="Straße", topics=["Straße"])
    respx.get(f"{API}/repos/search").mock(return_value=search_resp(a))
    p = ForgejoProvider("codeberg", API)
    assert len(await p.search("x", SearchFilters(language="STRASSE"))) == 1
    assert len(await p.search("x", SearchFilters(topic="STRASSE"))) == 1
    assert len(await p.search("x", SearchFilters(language="   "))) == 1
    assert len(await p.search("x", SearchFilters(language="Rust"))) == 0


@pytest.mark.parametrize("url", [
    "http://h.org/api/v1", "https://u:pw@h.org/api/v1", "https://u@h.org/api/v1", "https://h.org/api/v1?x=1",
    "https://h.org/api/v1#f", "ftp://h.org/api", "https://", "", "h.org/api/v1"])
def test_constructor_refuses_bad_base_url(url):
    with pytest.raises(ValueError) as ei:
        ForgejoProvider("x", url, token="tok")
    assert "pw" not in str(ei.value) and "tok" not in str(ei.value)


def test_constructor_accepts_normal_base_url():
    ForgejoProvider("x", "https://git.example.org/api/v1")
    ForgejoProvider("x", "https://git.example.org:3000/api/v1/")
