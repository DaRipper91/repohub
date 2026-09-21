import contextlib
import io
import json
import shlex
import subprocess

import pytest
from fastapi.testclient import TestClient

import repohub.cli as cli
from helpers import FakeProvider, make_hub, mk
from repohub.core.awareness import Awareness
from repohub.core.browse import Shelf
from repohub.core.external import find_tool, grab_command, release_command, repo_url
from repohub.core.hosts import BUILTIN_HOSTS, HostRegistry, HostSpec, reset_registry, set_registry
from repohub.core.machine import Machine
from repohub.core.models import Asset, Release
from repohub.web.app import create_app

MACHINE = Machine("arm64", "Linux", 7.0, frozenset())


@pytest.fixture(autouse=True)
def config_dir(monkeypatch, tmp_path):
    cfg = tmp_path / "xdg-config"
    cfg.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg))


# ---------------------------------------------------------------- commands

def test_urls_and_commands_for_every_kind_of_host():
    assert repo_url("github", "o/r") == "https://github.com/o/r"
    assert repo_url("gitlab", "g/sub/r") == "https://gitlab.com/g/sub/r"
    assert repo_url("codeberg", "o/r") == "https://codeberg.org/o/r"
    assert grab_command("github", "o/r") == "ghgrab https://github.com/o/r"
    assert grab_command("gitlab", "g/sub/r") == "ghgrab https://gitlab.com/g/sub/r"


@pytest.mark.parametrize("host,slug", [("github", "../x"), ("github", "o/r/extra"), ("nope", "o/r"), ("github", "o/" + "r" * 300),
                                       ("github", "o/r\nrm -rf ~"), ("github", "o/r;ls"), ("github", "$(id)/r"), ("github", "")])
def test_anything_that_is_not_a_repository_gets_no_command(host, slug):
    assert repo_url(host, slug) is None and grab_command(host, slug) is None and release_command(host, slug) is None


def test_release_command_is_github_only():
    assert release_command("github", "sharkdp/bat") == "ghgrab rel sharkdp/bat"
    assert release_command("gitlab", "g/r") is None and release_command("codeberg", "o/r") is None


def test_configured_extra_hosts_work_and_are_quoted():
    set_registry(HostRegistry(BUILTIN_HOSTS + (HostSpec("myforge", "forgejo", "M", "git.example.org", "https://git.example.org/api/v1"),)))
    try:
        assert grab_command("myforge", "o/r") == "ghgrab https://git.example.org/o/r"
    finally:
        reset_registry()


def test_commands_survive_shell_parsing_as_exactly_two_words():
    for host, slug in (("github", "o/r"), ("gitlab", "a/b/c")):
        assert len(shlex.split(grab_command(host, slug))) == 2


# ---------------------------------------------------------------- finding the tool

def test_find_tool_accepts_only_absolute_paths_outside_the_avoided_folder(tmp_path):
    assert find_tool("x", which=lambda n: "/usr/bin/x") == "/usr/bin/x"
    assert find_tool("x", which=lambda n: None) is None
    assert find_tool("x", which=lambda n: "relative/x") is None and find_tool("x", which=lambda n: "./x") is None
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "x").write_text("")
    assert find_tool("x", avoid=str(repo), which=lambda n: str(repo / "x")) is None
    link = tmp_path / "link"
    link.symlink_to(repo)
    assert find_tool("x", avoid=str(repo), which=lambda n: str(link / "x")) is None  # a symlink into it is refused too
    assert find_tool("x", avoid=str(repo), which=lambda n: "/usr/bin/x") == "/usr/bin/x"


def test_find_tool_looks_up_path_at_call_time(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda n: "/opt/bin/" + n)
    assert find_tool("ghgrab") == "/opt/bin/ghgrab"


# ---------------------------------------------------------------- web

def web(tmp_path, monkeypatch, installed=True, release=True, host="github"):
    monkeypatch.setattr("shutil.which", lambda n: "/usr/bin/ghgrab" if installed and n == "ghgrab" else None)
    rel = Release("v1", None, (Asset("t-arm64.tgz", 1, "u", "arm64"),)) if release else None
    gh = FakeProvider(host, [mk(host, "o/r", 5)], release=rel)
    app = create_app(make_hub(gh), tmp_path, session_token="t", shelves=[Shelf("s", topic="x")],
                     awareness=Awareness(tmp_path, MACHINE))
    return TestClient(app, base_url="http://localhost")


def test_repo_page_shows_the_commands(tmp_path, monkeypatch):
    t = web(tmp_path, monkeypatch).get("/repo/github/o/r").text
    assert "Grab files with ghgrab" in t and "ghgrab https://github.com/o/r" in t and "ghgrab rel o/r" in t
    assert "not installed here" not in t


def test_repo_page_tells_you_how_to_install_ghgrab_when_missing(tmp_path, monkeypatch):
    t = web(tmp_path, monkeypatch, installed=False).get("/repo/github/o/r").text
    assert "not installed here" in t and "pipx install ghgrab" in t and "ghgrab https://github.com/o/r" in t


def test_release_command_only_with_a_release_and_only_on_github(tmp_path, monkeypatch):
    assert "ghgrab rel" not in web(tmp_path, monkeypatch, release=False).get("/repo/github/o/r").text
    assert "ghgrab rel" not in web(tmp_path, monkeypatch, host="gitlab").get("/repo/gitlab/o/r").text


def test_the_page_never_runs_anything(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("nothing may be started by the web app"))
    web(tmp_path, monkeypatch).get("/repo/github/o/r")


# ---------------------------------------------------------------- CLI

def run(argv):
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(argv, hub_factory=lambda: pytest.fail("no hub"), stdout=out, stderr=err, gh_cli=lambda: None)
    return code, out.getvalue(), err.getvalue()


def test_cli_grab_prints_commands_and_runs_nothing(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda n: None)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("the CLI must not run anything"))
    code, out, err = run(["grab", "github:o/r"])
    assert code == 0 and out.splitlines()[0] == "ghgrab https://github.com/o/r" and "ghgrab rel o/r" in out
    assert "not installed" in err and "pipx install ghgrab" in err


def test_cli_grab_json_and_errors(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda n: "/usr/bin/ghgrab")
    code, out, _ = run(["grab", "gitlab:g/r", "--json"])
    doc = json.loads(out)
    assert code == 0 and doc["command"] == "ghgrab https://gitlab.com/g/r" and doc["release_command"] is None
    assert doc["ghgrab_installed"] is True
    assert run(["grab", "nonsense"])[0] == 2 and run(["grab", "github:../x"])[0] == 2


# ---------------------------------------------------------------- terminal app

async def _detail(tmp_path, monkeypatch, launcher, installed=True, host="github"):
    from repohub.tui.app import DetailScreen, RepoHubApp

    monkeypatch.setattr("shutil.which", lambda n: "/usr/bin/ghgrab" if installed and n == "ghgrab" else None)
    gh = FakeProvider(host, [mk(host, "o/r", 5)])
    app = RepoHubApp(make_hub(gh), tmp_path, shelves=[Shelf("s", topic="x")], awareness=Awareness(tmp_path, MACHINE), launcher=launcher)
    return app, DetailScreen(app.hub, host, "o/r", tmp_path, app.cloner)


async def test_tui_grab_asks_first_and_launches_ghgrab_with_the_url(tmp_path, monkeypatch):
    import os

    from repohub.tui.app import ConfirmWrite

    calls = []
    app, detail = await _detail(tmp_path, monkeypatch, lambda argv, cwd=None: calls.append((argv, cwd)))
    monkeypatch.setattr(type(app), "suspend", lambda self: contextlib.nullcontext())
    async with app.run_test() as pilot:
        app.push_screen(detail)
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmWrite) and "https://github.com/o/r" in app.screen.text and calls == []
        await pilot.press("n")
        await pilot.pause()
        assert calls == []
        await pilot.press("g")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        assert calls == [(["/usr/bin/ghgrab", "https://github.com/o/r"], os.path.expanduser("~"))]


async def test_tui_grab_without_ghgrab_only_explains(tmp_path, monkeypatch):
    from repohub.tui.app import ConfirmWrite

    calls = []
    app, detail = await _detail(tmp_path, monkeypatch, lambda *a, **k: calls.append(1), installed=False)
    async with app.run_test() as pilot:
        app.push_screen(detail)
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        assert not isinstance(app.screen, ConfirmWrite) and calls == []


async def test_tui_grab_falls_back_to_showing_the_command_when_it_cannot_suspend(tmp_path, monkeypatch):
    calls = []
    app, detail = await _detail(tmp_path, monkeypatch, lambda *a, **k: calls.append(1))
    async with app.run_test() as pilot:  # the headless driver cannot suspend
        app.push_screen(detail)
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        assert calls == []


async def test_tui_grab_reports_a_failed_launch_without_crashing(tmp_path, monkeypatch):
    def boom(argv, cwd=None):
        raise OSError("exec format error")

    app, detail = await _detail(tmp_path, monkeypatch, boom)
    monkeypatch.setattr(type(app), "suspend", lambda self: contextlib.nullcontext())
    async with app.run_test() as pilot:
        app.push_screen(detail)
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        assert app.is_running  # the error was shown, not raised
