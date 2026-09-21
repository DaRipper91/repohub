"""Cross-front-end consistency and offline checks that span several modules."""
import hashlib
import io
import json
import re
from dataclasses import asdict

from fastapi.testclient import TestClient

import repohub.cli as cli
from helpers import FakeProvider, make_hub, mk
from repohub.core.browse import LoadedShelves, Shelf, ShelfEntry
from repohub.core.hub import DETAIL_TTL, SEARCH_TTL
from repohub.core.models import SearchFilters
from repohub.tui.app import RepoHubApp
from repohub.web.app import create_app


def shelf_list():
    cur = lambda n: Shelf(n, repos=(ShelfEntry("github", "o/r"),), as_of="2026-09-01")  # noqa: E731
    return [Shelf("S0", topic="a"), cur("C1"), Shelf("S2", topic="b"), cur("C3"), Shelf("S4", query="q")]


async def test_shelf_indexes_agree_in_web_tui_and_cli(tmp_path, monkeypatch):
    shelves = shelf_list()
    names = [s.name for s in shelves]
    # web: home order and /shelves/{i} links
    app = create_app(make_hub(FakeProvider("github")), tmp_path, session_token="t", shelves=shelves)
    html = TestClient(app, base_url="http://localhost").get("/").text
    web = re.findall(r"<h2>(.*?) <a class=\"dim\" href=\"/shelves/(\d+)\">", html)
    assert [(n, int(i)) for n, i in web] == list(zip(names, range(5)))
    # tui: row keys
    from textual.widgets import DataTable
    tui = RepoHubApp(make_hub(FakeProvider("github")), tmp_path, shelves=shelves)
    async with tui.run_test() as pilot:
        await pilot.pause()
        assert [k.value for k in tui.query_one(DataTable).rows] == [f"shelf:{i}" for i in range(5)]
    # cli
    monkeypatch.setattr(cli, "load_all_shelves", lambda *a, **k: LoadedShelves(list(shelves), []))
    out = io.StringIO()
    assert cli.main(["shelves", "--json"], stdout=out, stderr=io.StringIO()) == 0
    assert [(e["index"], e["name"]) for e in json.loads(out.getvalue())["shelves"]] == list(enumerate(names))


def test_real_packaged_catalog_shelf_offline_via_cli(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    gh = FakeProvider("github")
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(["shelf", "Catalog: Terminal & TUI", "--limit", "3", "--no-refresh", "--json"],
                    hub_factory=lambda: make_hub(gh), stdout=out, stderr=err)
    doc = json.loads(out.getvalue())
    assert code == 0 and gh.calls == 0
    assert doc["shelf"] == "Catalog: Terminal & TUI" and len(doc["repos"]) == 3
    assert all(r["live"] is False and r["as_of"] == "2026-09-20" for r in doc["repos"])


def _old_row(repo):
    d = repo.to_dict()
    d.pop("fork")
    return d


async def test_old_cache_rows_without_fork_key_still_load():
    r = mk("github", "o/r", 7)
    hub = make_hub(FakeProvider("github"))
    # search
    filters, query = SearchFilters(), "x"
    digest = hashlib.sha256(json.dumps([query, asdict(filters)], sort_keys=True).encode()).hexdigest()
    hub.cache.set(f"search:{digest}", {"repos": [_old_row(r)], "errors": {}}, SEARCH_TTL)
    res = await hub.search(query, filters)
    assert res.repos[0].slug == "o/r" and res.repos[0].fork is False
    # repo summary
    hub.cache.set("repo:github:o/r", _old_row(r), DETAIL_TTL)
    assert (await hub.repo_summary("github", "O/R")).fork is False
    # detail
    hub.cache.set("detail:github:o/r", {"repo": _old_row(r), "readme": "x", "release": None}, DETAIL_TTL)
    assert (await hub.detail("github", "o/r")).repo.fork is False
