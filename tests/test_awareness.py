import io
import json
import os

import pytest
from fastapi.testclient import TestClient

import repohub.cli as cli
from helpers import FakeProvider, make_hub, mk
from repohub.core import runcheck as rc
from repohub.core.awareness import Awareness
from repohub.core.browse import Shelf
from repohub.core.clonescan import MAX_CONFIG_BYTES, MAX_FOLDERS, CloneInfo, parse_remote, scan_clones
from repohub.core.detect import detect_project
from repohub.core.machine import Machine, arch_tag, read_machine
from repohub.core.models import Asset, Release
from repohub.web.app import create_app


@pytest.fixture(autouse=True)
def config_dir(monkeypatch, tmp_path):
    cfg = tmp_path / "xdg-config"
    cfg.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg))


def make_clone(root, name, url, extra=""):
    d = root / name / ".git"
    d.mkdir(parents=True)
    (d / "config").write_text(f'[core]\n\trepositoryformatversion = 0\n[remote "origin"]\n\turl = {url}\n\tfetch = +refs/heads/*:refs/remotes/origin/*\n{extra}')
    return root / name


# ---------------------------------------------------------------- remote URL parsing

@pytest.mark.parametrize("url,expected", [
    ("https://github.com/o/r.git", ("github", "o/r")),
    ("https://github.com/o/r", ("github", "o/r")),
    ("https://user:pw@github.com/o/r.git", ("github", "o/r")),
    ("git@github.com:o/r.git", ("github", "o/r")),
    ("ssh://git@codeberg.org/o/r.git", ("codeberg", "o/r")),
    ("https://gitlab.com/g/sub/r.git", ("gitlab", "g/sub/r")),
    ("HTTPS://GitHub.COM/o/r/", ("github", "o/r")),
])
def test_parse_remote_accepts_configured_hosts(url, expected):
    assert parse_remote(url) == expected


@pytest.mark.parametrize("url", [
    "", "https://evil.example/o/r.git", "https://github.com.evil.example/o/r", "https://evil.example/github.com/o/r",
    "file:///etc/passwd", "/home/x/repo", "../x", "ftp://github.com/o/r", "https://github.com/o", "https://github.com/../x",
    "https://github.com/o/r\nx", "git@evil.example:o/r.git", "https://github.com/o/r/extra/deep", "x" * 400])
def test_parse_remote_rejects_everything_else(url):
    assert parse_remote(url) is None


# ---------------------------------------------------------------- scanning

def test_scan_finds_clones_by_origin_not_folder_name(tmp_path):
    make_clone(tmp_path, "renamed-folder", "https://github.com/O/R.git")
    make_clone(tmp_path, "other", "git@codeberg.org:me/tool.git")
    found = scan_clones(tmp_path)
    assert set(found) == {"github:o/r", "codeberg:me/tool"}
    assert found["github:o/r"].path == str(tmp_path / "renamed-folder")


def test_scan_ignores_unknown_hosts_missing_git_and_files(tmp_path):
    make_clone(tmp_path, "evil", "https://evil.example/o/r.git")
    (tmp_path / "plain").mkdir()
    (tmp_path / "file.txt").write_text("x")
    (tmp_path / ".hidden").mkdir()
    make_clone(tmp_path, ".dot", "https://github.com/o/dot.git")
    assert scan_clones(tmp_path) == {}


def test_scan_uses_origin_only(tmp_path):
    d = tmp_path / "x" / ".git"
    d.mkdir(parents=True)
    (d / "config").write_text('[remote "upstream"]\n\turl = https://github.com/up/stream.git\n[remote "origin"]\n\turl = https://github.com/me/mine.git\n')
    assert set(scan_clones(tmp_path)) == {"github:me/mine"}
    (d / "config").write_text('[remote "upstream"]\n\turl = https://github.com/up/stream.git\n')
    assert scan_clones(tmp_path) == {}


def test_scan_never_follows_symlinks(tmp_path):
    outside = tmp_path / "outside"
    make_clone(outside, "secret", "https://github.com/o/secret.git")
    root = tmp_path / "root"
    root.mkdir()
    os.symlink(outside / "secret", root / "linked-folder")  # a symlinked repo folder
    real = make_clone(root, "real", "https://github.com/o/real.git")
    os.rename(real / ".git" / "config", tmp_path / "cfg-elsewhere")
    os.symlink(tmp_path / "cfg-elsewhere", real / ".git" / "config")  # a symlinked config file
    assert scan_clones(root) == {}


def test_scan_skips_oversized_config_and_git_files(tmp_path):
    d = make_clone(tmp_path, "big", "https://github.com/o/big.git")
    (d / ".git" / "config").write_text("#" * (MAX_CONFIG_BYTES + 1) + '\n[remote "origin"]\nurl = https://github.com/o/big.git\n')
    sub = tmp_path / "worktree"
    sub.mkdir()
    (sub / ".git").write_text("gitdir: /elsewhere")  # a .git FILE (worktree/submodule): skipped
    assert scan_clones(tmp_path) == {}


def test_scan_caps_folder_count(tmp_path):
    for i in range(MAX_FOLDERS + 20):
        make_clone(tmp_path, f"r{i:04d}", f"https://github.com/o/r{i}.git")
    assert len(scan_clones(tmp_path)) == MAX_FOLDERS


def test_scan_missing_or_unreadable_root_is_empty(tmp_path):
    assert scan_clones(tmp_path / "nope") == {}
    f = tmp_path / "afile"
    f.write_text("x")
    assert scan_clones(f) == {}


def test_scan_tolerates_binary_garbage(tmp_path):
    d = tmp_path / "g" / ".git"
    d.mkdir(parents=True)
    (d / "config").write_bytes(b"\xff\xfe\x00\x01[remote \"origin\"\n url=")
    assert scan_clones(tmp_path) == {}


# ---------------------------------------------------------------- detector

def test_detect_project_by_file_names_only(tmp_path):
    for n in ("Cargo.toml", "Makefile", "README.md", "requirements.txt"):
        (tmp_path / n).write_text("secret contents are never read")
    assert detect_project(tmp_path) == ["make", "python", "rust"]
    assert detect_project(tmp_path / "missing") == []


def test_detect_does_not_look_in_subfolders(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "Cargo.toml").write_text("")
    assert detect_project(tmp_path) == []


# ---------------------------------------------------------------- machine

def test_arch_tag():
    assert arch_tag("aarch64") == "arm64" and arch_tag("arm64") == "arm64" and arch_tag("AMD64") == "x86_64"
    assert arch_tag("riscv64") == "riscv64" and arch_tag("") == "unknown"


def test_read_machine_only_asks_which_and_never_runs_anything():
    asked = []

    def which(name):
        asked.append(name)
        return f"/usr/bin/{name}" if name in ("cargo", "git") else None

    m = read_machine(which)
    assert {"cargo", "git"} <= m.tools and "go" not in m.tools and "cargo" in asked


# ---------------------------------------------------------------- verdict

ARM = Machine("arm64", "Linux", 7.0, frozenset({"cargo", "git", "python3", "make"}))
X86_ASSET = Asset("t-x86_64.tgz", 1, "https://x/t", "x86_64")
ARM_ASSET = Asset("t-arm64.tgz", 1, "https://x/t", "arm64")


def checks(v):
    return {c.label: c for c in v.checks}


def test_verdict_likely_when_arm64_build_exists():
    v = rc.run_check(mk(language="Go"), Release("v1", None, (ARM_ASSET, X86_ASSET)), ARM)
    assert v.level == rc.LIKELY and checks(v)["Ready-made build"].status == rc.OK


def test_verdict_x86_only_release_with_tools_is_maybe_with_no_check():
    v = rc.run_check(mk(language="Rust"), Release("v1", None, (X86_ASSET,)), ARM)
    assert checks(v)["Ready-made build"].status == rc.NO and v.level == rc.MAYBE


def test_verdict_x86_only_release_without_tools_needs_setup():
    v = rc.run_check(mk(language="Go"), Release("v1", None, (X86_ASSET,)), ARM)
    assert v.level == rc.SETUP and "go" in v.summary


def test_verdict_unlikely_when_only_wrong_cpu_and_kind_unknown():
    v = rc.run_check(mk(language=""), Release("v1", None, (X86_ASSET,)), ARM)
    assert v.level == rc.UNLIKELY


def test_verdict_maybe_from_source_with_tools_and_needs_setup_without():
    assert rc.run_check(mk(language="Rust"), None, ARM).level == rc.MAYBE
    v = rc.run_check(mk(language="Java"), None, ARM)
    assert v.level == rc.SETUP and checks(v)["Tools for java"].status == rc.NO


def test_verdict_unknown_without_any_information():
    v = rc.run_check(mk(language=""), None, ARM)
    assert v.level == rc.UNKNOWN


def test_detected_files_beat_the_language_guess_and_cloned_is_reported():
    clone = CloneInfo("github", "o/r", "/home/x/playground/r")
    v = rc.run_check(mk(language="Go"), None, ARM, clone, ["rust"])
    c = checks(v)
    assert "Tools for rust" in c and "Tools for go" not in c
    assert c["Already cloned"].status == rc.OK and "/home/x/playground/r" in c["Already cloned"].detail
    assert "found in the cloned folder" in c["Project type"].detail


def test_unknown_release_arch_is_a_warning_not_a_no():
    v = rc.run_check(mk(language="Rust"), Release("v1", None, (Asset("t.tgz", 1, "u", "unknown"),)), ARM)
    assert checks(v)["Ready-made build"].status == rc.WARN


def test_verdict_text_is_sanitised():
    v = rc.run_check(mk(language="Rust"), Release("v1\x1b[31m", None, (ARM_ASSET,)), ARM)
    assert "\x1b" not in json.dumps(v.to_dict())


# ---------------------------------------------------------------- awareness cache

def test_awareness_rescans_after_ttl_and_never_raises(tmp_path):
    now = [0.0]
    calls = []

    def scan(root):
        calls.append(1)
        return {}

    a = Awareness(tmp_path, ARM, scan=scan, clock=lambda: now[0])
    a.cloned()
    a.cloned()
    assert len(calls) == 1
    now[0] += 11
    a.cloned()
    assert len(calls) == 2

    def boom(root):
        raise RuntimeError("x")

    assert Awareness(tmp_path, ARM, scan=boom).cloned() == {}


# ---------------------------------------------------------------- web

def web(tmp_path, repos=None, release=None):
    gh = FakeProvider("github", repos or [mk("github", "o/r", 5, language="Rust")], release=release)
    hub = make_hub(gh)
    root = tmp_path / "clones"
    root.mkdir(exist_ok=True)
    app = create_app(hub, root, session_token="t", shelves=[Shelf("s", topic="x")], awareness=Awareness(root, ARM))
    return TestClient(app, base_url="http://localhost"), root


def test_repo_page_shows_checklist_and_verdict(tmp_path):
    c, _ = web(tmp_path, release=Release("v1", None, (ARM_ASSET,)))
    t = c.get("/repo/github/o/r").text
    assert "Can I run this here?" in t and "Likely" in t and "Ready-made build" in t and "never runs anything" in t
    assert "cloned</span>" not in t


def test_cloned_badge_on_repo_page_search_and_favorites(tmp_path):
    c, root = web(tmp_path)
    make_clone(root, "whatever", "https://github.com/o/r.git")
    (root / "whatever" / "Cargo.toml").write_text("")
    page = c.get("/repo/github/o/r").text
    assert 'title="Found in your clone folder">cloned' in page and "found in the cloned folder" in page
    assert "cloned</span>" in c.get("/search", params={"q": "x"}).text


def test_web_never_shows_badge_for_unrelated_repo(tmp_path):
    c, root = web(tmp_path)
    make_clone(root, "other", "https://github.com/z/other.git")
    assert "cloned</span>" not in c.get("/repo/github/o/r").text


def test_hostile_clone_folder_names_and_urls_cannot_inject(tmp_path):
    c, root = web(tmp_path)
    make_clone(root, "<script>alert(1)</script>", "https://github.com/o/r.git")
    t = c.get("/repo/github/o/r").text
    assert "<script>alert(1)</script>" not in t


# ---------------------------------------------------------------- CLI

def run(argv, hub=None, monkeypatch=None):
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(argv, hub_factory=lambda: hub, stdout=out, stderr=err, gh_cli=lambda: None)
    return code, out.getvalue(), err.getvalue()


def test_cli_cloned_lists_without_a_hub_or_network(tmp_path, monkeypatch):
    make_clone(tmp_path, "a", "https://github.com/o/r.git")
    monkeypatch.setenv("REPOHUB_CLONE_DIR", str(tmp_path))
    code, out, _ = run(["cloned", "--json"])  # hub=None: a hub build would raise
    assert code == 0 and json.loads(out)["cloned"] == [{"host": "github", "slug": "o/r", "path": str(tmp_path / "a")}]
    code, out, _ = run(["cloned"])
    assert "o/r" in out


def test_cli_cloned_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("REPOHUB_CLONE_DIR", str(tmp_path))
    code, out, err = run(["cloned"])
    assert code == 0 and out == "" and "no cloned repositories" in err


def test_cli_check_json_and_text(tmp_path, monkeypatch):
    monkeypatch.setenv("REPOHUB_CLONE_DIR", str(tmp_path))
    monkeypatch.setattr("repohub.core.awareness.read_machine", lambda: ARM)
    hub = make_hub(FakeProvider("github", [mk("github", "o/r", 5, language="Rust")], release=Release("v1", None, (ARM_ASSET,))))
    code, out, _ = run(["check", "github:o/r", "--json"], hub)
    doc = json.loads(out)
    assert code == 0 and doc["verdict"] == "likely" and doc["repo"] == "github:o/r" and doc["checks"]
    code, out, _ = run(["check", "github:o/r"], hub)
    assert "LIKELY" in out and "[+] Ready-made build" in out


def test_cli_check_bad_argument_and_missing_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("REPOHUB_CLONE_DIR", str(tmp_path))
    assert run(["check", "nonsense"])[0] == 2
    hub = make_hub(FakeProvider("github", [], detail_error=cli.NotFound("github", "x")))
    assert run(["check", "github:o/none"], hub)[0] == 1


def test_cli_new_commands_do_not_write_anything(tmp_path, monkeypatch):
    make_clone(tmp_path, "a", "https://github.com/o/r.git")
    before = sorted(p.name for p in tmp_path.rglob("*"))
    monkeypatch.setenv("REPOHUB_CLONE_DIR", str(tmp_path))
    run(["cloned"])
    assert sorted(p.name for p in tmp_path.rglob("*")) == before


# ---------------------------------------------------------------- TUI

async def test_tui_marks_cloned_repos_and_shows_checklist(tmp_path):
    from textual.widgets import DataTable, Input, Static

    from repohub.tui.app import DetailScreen, RepoHubApp

    root = tmp_path / "clones"
    root.mkdir()
    make_clone(root, "x", "https://github.com/o/r.git")
    gh = FakeProvider("github", [mk("github", "o/r", 50, language="Rust"), mk("github", "p/q", 5)],
                      release=Release("v1", None, (ARM_ASSET,)))
    app = RepoHubApp(make_hub(gh), root, shelves=[Shelf("s", topic="x")], awareness=Awareness(root, ARM))
    async with app.run_test() as pilot:
        app.query_one(Input).focus()
        await pilot.press(*"tui", "enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        t = app.query_one(DataTable)
        cells = [str(t.get_row_at(i)[0]) for i in range(t.row_count)]
        assert "● o/r" in cells and "p/q" in cells
        app.push_screen(DetailScreen(app.hub, "github", "o/r", root, app.cloner))
        await app.workers.wait_for_complete()
        await pilot.pause()
        text = str(app.screen.query_one("#meta", Static).render())
        assert "Can I run this here? LIKELY" in text and "Ready-made build" in text
