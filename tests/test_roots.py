import io
import json
import os

import pytest
from fastapi.testclient import TestClient

import repohub.cli as cli
from helpers import FakeProvider, make_hub, mk
from repohub.core import roots as rt
from repohub.core.awareness import Awareness
from repohub.core.browse import Shelf
from repohub.core.machine import Machine
from repohub.core.roots import MAX_ROOTS, Candidate, ScanReport, ScanRoots, discover, valid_root
from repohub.web.app import create_app

MACHINE = Machine("arm64", "Linux", 7.0, frozenset())


@pytest.fixture(autouse=True)
def config_dir(monkeypatch, tmp_path):
    cfg = tmp_path / "xdg-config"
    cfg.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg))


def clone(parent, name, url="https://github.com/o/r.git"):
    d = parent / name / ".git"
    d.mkdir(parents=True)
    (d / "config").write_text(f'[remote "origin"]\n\turl = {url}\n')
    return parent / name


# ---------------------------------------------------------------- valid_root

@pytest.mark.parametrize("bad", ["", "relative/dir", "~nobody-such/x", "/", "/proc", "/sys/kernel", "/etc", "/etc/ssh", "/dev",
                                 "/usr/lib", "/var/log", "/var/lib/docker", "bad\x00nul", "bad\nline", None, 5, "/" + "a" * 5000])
def test_valid_root_rejects_unsafe_values(bad):
    assert valid_root(bad) is None


def test_valid_root_accepts_existing_folder_and_resolves(tmp_path):
    (tmp_path / "real").mkdir()
    os.symlink(tmp_path / "real", tmp_path / "link")
    assert valid_root(str(tmp_path / "link")) == (tmp_path / "real").resolve()
    assert valid_root(str(tmp_path / "missing")) is None
    (tmp_path / "f").write_text("x")
    assert valid_root(str(tmp_path / "f")) is None


def test_symlink_into_a_forbidden_tree_is_rejected(tmp_path):
    os.symlink("/etc", tmp_path / "sneaky")
    assert valid_root(str(tmp_path / "sneaky")) is None


# ---------------------------------------------------------------- ScanRoots

def test_roots_add_list_remove_roundtrip(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    s = ScanRoots(tmp_path / "roots.json")
    assert s.list() == []
    assert s.add([str(tmp_path / "a"), str(tmp_path / "b"), str(tmp_path / "a")]) == [str(tmp_path / "a"), str(tmp_path / "b")]
    assert s.list() == [str(tmp_path / "a"), str(tmp_path / "b")]
    assert s.remove(str(tmp_path / "a")) is True and s.remove(str(tmp_path / "a")) is False
    assert s.list() == [str(tmp_path / "b")]


def test_roots_add_ignores_invalid_and_caps(tmp_path):
    s = ScanRoots(tmp_path / "roots.json")
    dirs = []
    for i in range(MAX_ROOTS + 5):
        d = tmp_path / f"d{i}"
        d.mkdir()
        dirs.append(str(d))
    assert s.add(["/etc", "/", "nope", *dirs]) == dirs[:MAX_ROOTS]
    assert len(s.list()) == MAX_ROOTS


def test_roots_file_only_stores_paths_and_leaves_no_temp_files(tmp_path):
    (tmp_path / "a").mkdir()
    cfg = tmp_path / "conf"
    ScanRoots(cfg / "roots.json").add([str(tmp_path / "a")])
    assert json.loads((cfg / "roots.json").read_text()) == {"roots": [str(tmp_path / "a")]}
    assert [p.name for p in cfg.iterdir()] == ["roots.json"]


def test_roots_drops_entries_that_vanished_or_became_unsafe(tmp_path):
    (tmp_path / "keep").mkdir()
    f = tmp_path / "roots.json"
    f.write_text(json.dumps({"roots": [str(tmp_path / "keep"), str(tmp_path / "gone"), "/etc", "rel", 7, None]}))
    assert ScanRoots(f).list() == [str(tmp_path / "keep")]


@pytest.mark.parametrize("content", ["not json", "[]", '{"roots": "x"}', '{"roots": {"a": 1}}', "", "\x00\x01"])
def test_roots_damaged_file_gives_no_roots(tmp_path, content):
    f = tmp_path / "roots.json"
    f.write_text(content)
    assert ScanRoots(f).list() == []


def test_roots_oversized_symlinked_or_fifo_file_is_ignored(tmp_path):
    (tmp_path / "a").mkdir()
    good = json.dumps({"roots": [str(tmp_path / "a")]})
    big = tmp_path / "big.json"
    big.write_text(good + " " * (rt.MAX_FILE_BYTES + 1))
    assert ScanRoots(big).list() == []
    real = tmp_path / "real.json"
    real.write_text(good)
    os.symlink(real, tmp_path / "link.json")
    assert ScanRoots(tmp_path / "link.json").list() == []
    os.mkfifo(tmp_path / "fifo.json")
    assert ScanRoots(tmp_path / "fifo.json").list() == []  # returns at once


# ---------------------------------------------------------------- discover

def test_discover_reports_parents_with_recognised_clones_ranked(tmp_path):
    clone(tmp_path / "work", "a")
    clone(tmp_path / "work", "b", "git@codeberg.org:me/b.git")
    clone(tmp_path / "other", "c")
    clone(tmp_path / "deep" / "er", "d")
    rep = discover(tmp_path)
    assert [(c.path, c.repos) for c in rep.candidates] == [
        (str(tmp_path / "work"), 2), (str(tmp_path / "deep" / "er"), 1), (str(tmp_path / "other"), 1)]
    assert rep.candidates[0].examples == ("a", "b") and not rep.truncated and rep.dirs_seen > 0


def test_discover_ignores_unrecognised_hosts_and_plain_git_without_origin(tmp_path):
    clone(tmp_path / "x", "evil", "https://evil.example/o/r.git")
    (tmp_path / "y" / "noremote" / ".git").mkdir(parents=True)
    (tmp_path / "y" / "noremote" / ".git" / "config").write_text("[core]\n")
    assert discover(tmp_path).candidates == []


def test_discover_skips_hidden_build_and_symlinked_folders(tmp_path):
    clone(tmp_path / ".hidden", "a")
    clone(tmp_path / "node_modules", "b")
    outside = tmp_path / "outside"
    clone(outside, "c")
    root = tmp_path / "root"
    root.mkdir()
    os.symlink(outside, root / "linked")
    assert discover(root).candidates == []


def test_discover_does_not_descend_into_a_recognised_clone(tmp_path):
    outer = clone(tmp_path / "w", "outer")
    clone(outer, "nested-inside")
    rep = discover(tmp_path)
    assert [(c.path, c.repos) for c in rep.candidates] == [(str(tmp_path / "w"), 1)]


def test_discover_walks_through_a_git_folder_that_is_not_a_clone(tmp_path):
    """A home folder kept in git must not stop the scan."""
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("[core]\n")
    clone(tmp_path / "Projects", "a")
    assert [c.path for c in discover(tmp_path).candidates] == [str(tmp_path / "Projects")]


def test_discover_limits_depth_dirs_and_time(tmp_path):
    clone(tmp_path / "a" / "b" / "c" / "d", "r")
    assert discover(tmp_path, max_depth=2).candidates == []
    assert len(discover(tmp_path, max_depth=6).candidates) == 1
    for i in range(30):
        (tmp_path / f"f{i}").mkdir()
    r = discover(tmp_path, max_dirs=5)
    assert r.truncated and r.dirs_seen <= 5
    ticks = iter(range(0, 1000))
    r = discover(tmp_path, budget=3, clock=lambda: float(next(ticks)))
    assert r.truncated


def test_discover_never_enters_forbidden_paths(tmp_path):
    seen = []
    real = os.scandir

    def spy(path):
        seen.append(str(path))
        return real(path)

    import repohub.core.roots as mod

    orig = mod.os.scandir
    mod.os.scandir = spy
    try:
        discover("/proc", max_depth=1)
        discover("/etc", max_depth=1)
    finally:
        mod.os.scandir = orig
    assert seen == []


def test_discover_missing_start_is_empty(tmp_path):
    assert discover(tmp_path / "nope").candidates == []


def test_discover_hostile_names_are_data_not_markup(tmp_path):
    clone(tmp_path / "<b>x<b>", "<img src=x onerror=alert(1)>")
    c = discover(tmp_path).candidates[0]
    assert c.path.endswith("<b>x<b>")  # kept as-is: escaping is the templates' job


# ---------------------------------------------------------------- awareness with several roots

def test_awareness_merges_roots_clone_folder_first(tmp_path):
    main, extra = tmp_path / "main", tmp_path / "extra"
    clone(main, "one", "https://github.com/o/dup.git")
    clone(extra, "two", "https://github.com/o/dup.git")
    clone(extra, "three", "https://github.com/o/other.git")
    a = Awareness(main, MACHINE, extra_roots=lambda: [str(extra), str(main)])
    found = a.cloned()
    assert set(found) == {"github:o/dup", "github:o/other"}
    assert found["github:o/dup"].path == str(main / "one")  # the clone folder wins
    assert a.roots() == [main, str(extra)]


def test_awareness_tolerates_a_failing_root_list_and_invalidates(tmp_path):
    main = tmp_path / "m"
    clone(main, "a")

    def boom():
        raise RuntimeError("x")

    a = Awareness(main, MACHINE, extra_roots=boom)
    assert set(a.cloned()) == {"github:o/r"}
    clone(main, "b", "https://github.com/o/b.git")
    assert "github:o/b" not in a.cloned()  # cached
    a.invalidate()
    assert "github:o/b" in a.cloned()


# ---------------------------------------------------------------- web

TOKEN = "t"


def web(tmp_path, monkeypatch, candidates=None):
    main = tmp_path / "main"
    main.mkdir(exist_ok=True)
    store = ScanRoots(tmp_path / "roots.json")
    gh = FakeProvider("github", [mk("github", "o/r", 5)])
    aware = Awareness(main, MACHINE, extra_roots=store.list)
    app = create_app(make_hub(gh), main, session_token=TOKEN, shelves=[Shelf("s", topic="x")], awareness=aware, roots=store)
    scans = []

    def fake_discover(start):
        scans.append(str(start))
        return ScanReport(list(candidates or []), 10, False, 0.1, str(start))

    monkeypatch.setattr("repohub.web.app.discover", fake_discover)
    monkeypatch.setattr("repohub.web.app.home_start", lambda: tmp_path / "home")
    return TestClient(app, base_url="http://localhost"), store, aware, scans


def post(c, url, **data):
    return c.post(url, data={"token": TOKEN, **data}, follow_redirects=False)


def test_folders_page_lists_default_and_states_nothing_is_scanned(tmp_path, monkeypatch):
    c, _, _, scans = web(tmp_path, monkeypatch)
    t = c.get("/folders").text
    assert str(tmp_path / "main") in t and "Nothing is scanned until you press a scan button" in t and scans == []
    assert "Folders" in c.get("/").text


def test_scan_needs_token_and_valid_scope(tmp_path, monkeypatch):
    c, _, _, scans = web(tmp_path, monkeypatch)
    assert c.post("/folders/scan", data={"scope": "home"}).status_code == 403
    assert c.post("/folders/scan", data={"scope": "home", "token": "bad"}).status_code == 403
    assert post(c, "/folders/scan", scope="/etc").status_code == 404
    assert scans == []


def test_scan_home_and_system_use_the_right_start(tmp_path, monkeypatch):
    c, *_, scans = web(tmp_path, monkeypatch)
    assert post(c, "/folders/scan", scope="home").headers["location"] == "/folders"
    post(c, "/folders/scan", scope="system")
    assert scans == [str(tmp_path / "home"), "/"]


def test_results_are_shown_escaped_and_used_ones_marked(tmp_path, monkeypatch):
    real = tmp_path / "found"
    real.mkdir()
    c, store, *_ = web(tmp_path, monkeypatch, [Candidate(str(real), 3, ("<script>alert(1)</script>",)),
                                             Candidate(str(tmp_path / "main"), 1, ())])
    post(c, "/folders/scan", scope="home")
    t = c.get("/folders").text
    assert str(real) in t and "<script>alert(1)</script>" not in t and "already in use" in t


def test_add_only_accepts_folders_from_the_last_scan(tmp_path, monkeypatch):
    found, other = tmp_path / "found", tmp_path / "other"
    found.mkdir()
    other.mkdir()
    c, store, *_ = web(tmp_path, monkeypatch, [Candidate(str(found), 2, ())])
    assert post(c, "/folders/add", path=str(found)).status_code == 303 and store.list() == []  # no scan yet
    post(c, "/folders/scan", scope="home")
    post(c, "/folders/add", path=[str(found), str(other), "/etc"])  # forged extras are ignored
    assert store.list() == [str(found)]
    assert c.post("/folders/add", data={"path": str(found)}).status_code == 403


def test_adding_a_folder_makes_its_clones_show_up_as_cloned(tmp_path, monkeypatch):
    found = tmp_path / "found"
    clone(found, "x", "https://github.com/o/r.git")
    c, store, aware, _ = web(tmp_path, monkeypatch, [Candidate(str(found), 1, ("x",))])
    assert "cloned</span>" not in c.get("/repo/github/o/r").text
    post(c, "/folders/scan", scope="home")
    post(c, "/folders/add", path=str(found))
    assert "cloned</span>" in c.get("/repo/github/o/r").text
    post(c, "/folders/remove", path=str(found))
    assert store.list() == [] and "cloned</span>" not in c.get("/repo/github/o/r").text


def test_remove_needs_token_and_a_saved_folder(tmp_path, monkeypatch):
    c, *_ = web(tmp_path, monkeypatch)
    assert c.post("/folders/remove", data={"path": "/x"}).status_code == 403
    assert post(c, "/folders/remove", path="/etc").status_code == 404


def test_only_one_scan_at_a_time(tmp_path, monkeypatch):
    import repohub.web.app as mod

    c, *_ = web(tmp_path, monkeypatch)
    # simulate a scan in flight by making discover raise after flipping nothing: a second call while
    # running is refused. Reach the shared flag through a slow discover that starts a nested request.
    state = {}

    def slow(start):
        state["nested"] = post(c, "/folders/scan", scope="home").status_code
        return ScanReport([], 0, False, 0.0, str(start))

    monkeypatch.setattr(mod, "discover", slow)
    post(c, "/folders/scan", scope="home")
    assert state["nested"] == 429


def test_a_failing_scan_clears_the_running_flag(tmp_path, monkeypatch):
    import repohub.web.app as mod

    c, *_ = web(tmp_path, monkeypatch)
    monkeypatch.setattr(mod, "discover", lambda start: (_ for _ in ()).throw(RuntimeError("x")))
    with pytest.raises(RuntimeError):
        post(c, "/folders/scan", scope="home")
    monkeypatch.setattr(mod, "discover", lambda start: ScanReport([], 0, False, 0.0, str(start)))
    assert post(c, "/folders/scan", scope="home").status_code == 303


# ---------------------------------------------------------------- CLI

def run(argv):
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(argv, hub_factory=lambda: pytest.fail("no hub"), stdout=out, stderr=err, gh_cli=lambda: None)
    return code, out.getvalue(), err.getvalue()


def test_cli_roots_lists_without_scanning(tmp_path, monkeypatch):
    monkeypatch.setenv("REPOHUB_CLONE_DIR", str(tmp_path))
    monkeypatch.setattr("repohub.core.roots.discover", lambda *a, **k: pytest.fail("scan"))
    code, out, _ = run(["roots", "--json"])
    assert code == 0 and json.loads(out)["roots"] == [str(tmp_path)] and "scan" not in json.loads(out)


def test_cli_roots_scan_reports_and_saves_nothing(tmp_path, monkeypatch):
    home = tmp_path / "home"
    clone(home / "Projects", "a")
    monkeypatch.setenv("REPOHUB_CLONE_DIR", str(tmp_path / "clones"))
    monkeypatch.setattr("repohub.core.roots.home_start", lambda: home)
    code, out, err = run(["roots", "--scan", "--json"])
    doc = json.loads(out)
    assert code == 0 and doc["scan"]["found"] == [{"path": str(home / "Projects"), "repos": 1, "in_use": False}]
    assert not (tmp_path / "xdg-config" / "repohub" / "scan_roots.json").exists()
    code, out, err = run(["roots", "--scan"])
    assert "Projects" in out and "not used" in out and "scanned" in err


def test_cli_roots_system_needs_scan(tmp_path, monkeypatch):
    monkeypatch.setenv("REPOHUB_CLONE_DIR", str(tmp_path))
    assert run(["roots", "--system"])[0] == 2


def test_cloned_command_includes_picked_folders(tmp_path, monkeypatch):
    extra = tmp_path / "extra"
    clone(extra, "a")
    (tmp_path / "clones").mkdir()
    monkeypatch.setenv("REPOHUB_CLONE_DIR", str(tmp_path / "clones"))
    ScanRoots().add([str(extra)])  # the real (XDG-redirected) config location
    code, out, _ = run(["cloned", "--json"])
    assert [c["slug"] for c in json.loads(out)["cloned"]] == ["o/r"]


# ---------------------------------------------------------------- terminal app

async def test_tui_folders_view_scan_add_and_remove(tmp_path, monkeypatch):
    from textual.widgets import DataTable

    from repohub.tui.app import ConfirmWrite, RepoHubApp

    found = tmp_path / "found"
    found.mkdir()
    main = tmp_path / "main"
    main.mkdir()
    store = ScanRoots(tmp_path / "roots.json")
    monkeypatch.setattr("repohub.tui.app.discover", lambda start: ScanReport([Candidate(str(found), 2, ())], 4, False, 0.1, str(start)))
    app = RepoHubApp(make_hub(FakeProvider("github", [])), main, shelves=[Shelf("s", topic="x")],
                     awareness=Awareness(main, MACHINE, extra_roots=store.list), roots=store)
    async with app.run_test() as pilot:
        await pilot.press("f5")
        await pilot.pause()
        t = app.query_one(DataTable)
        assert app.view == "folders" and [k.value for k in t.rows] == ["root:default"]
        t.focus()
        await pilot.press("s")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert [k.value for k in app.query_one(DataTable).rows] == ["root:default", "cand:0"]
        app.query_one(DataTable).move_cursor(row=1)
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmWrite) and store.list() == []
        await pilot.press("y")
        await pilot.pause()
        assert store.list() == [str(found)]
        assert [k.value for k in app.query_one(DataTable).rows] == ["root:default", f"root:{found}"]
        app.query_one(DataTable).move_cursor(row=1)
        await pilot.press("d")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmWrite)
        await pilot.press("n")
        await pilot.pause()
        assert store.list() == [str(found)]
        await pilot.press("d")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        assert store.list() == []


async def test_tui_scan_keys_do_nothing_outside_the_folders_view(tmp_path, monkeypatch):
    from repohub.tui.app import RepoHubApp

    monkeypatch.setattr("repohub.tui.app.discover", lambda start: pytest.fail("scan"))
    main = tmp_path / "m"
    main.mkdir()
    app = RepoHubApp(make_hub(FakeProvider("github", [])), main, shelves=[Shelf("s", topic="x")],
                     awareness=Awareness(main, MACHINE), roots=ScanRoots(tmp_path / "r.json"))
    async with app.run_test() as pilot:
        app.query_one("DataTable").focus()
        await pilot.press("s", "w")
        await pilot.pause()
        assert app.view == "shelves"
