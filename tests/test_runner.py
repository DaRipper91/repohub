import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import repohub.cli as cli
from helpers import FakeProvider, make_hub, mk
from repohub.core import runner as rn
from repohub.core.actionlog import ActionLog
from repohub.core.awareness import Awareness
from repohub.core.browse import Shelf
from repohub.core.clonescan import CloneInfo
from repohub.core.machine import Machine
from repohub.core.runner import RunError, Runner, clean_output
from repohub.core.runplan import VENV, Plan, Step, build_plan
from repohub.core.settings import Settings
from repohub.web.app import create_app

MACHINE = Machine("arm64", "Linux", 7.0, frozenset())
PY = sys.executable


@pytest.fixture(autouse=True)
def config_dir(monkeypatch, tmp_path):
    cfg = tmp_path / "xdg-config"
    cfg.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg))


def make_clone(tmp_path, name="r", url="https://github.com/o/r.git"):
    d = tmp_path / "clones" / name
    (d / ".git").mkdir(parents=True, exist_ok=True)
    (d / ".git" / "config").write_text(f'[remote "origin"]\n\turl = {url}\n')
    return d


def setup(tmp_path, code="print('hi')", *, enabled=True, plan=None, **kw):
    d = make_clone(tmp_path)
    aw = Awareness(tmp_path / "clones", MACHINE)
    st = Settings()
    st.set("guided_run", enabled)
    step_plan = plan or Plan(str(d), (Step("t", "Test step", (PY, "-c", code)),))
    log = ActionLog()
    r = Runner(aw, st, log, plan_fn=lambda f: step_plan, environ=kw.pop("environ", {"PATH": os.environ["PATH"], "HOME": "/h"}), **kw)
    return r, step_plan, log, d


def wait(session, timeout=10):
    t0 = time.monotonic()
    while session.running and time.monotonic() - t0 < timeout:
        time.sleep(0.02)
    assert not session.running
    return session


# ---------------------------------------------------------------- settings

def test_settings_default_off_and_only_known_names():
    s = Settings()
    assert s.get("guided_run") is False
    s.set("guided_run", True)
    assert s.get("guided_run") is True
    with pytest.raises(ValueError):
        s.set("anything_else", True)
    with pytest.raises(ValueError):
        s.get("nope")


def test_settings_persist(tmp_path):
    db = str(tmp_path / "s.db")
    Settings(db).set("guided_run", True)
    assert Settings(db).get("guided_run") is True


# ---------------------------------------------------------------- the fixed plan

def ids(plan):
    return [s.id for s in plan.steps]


def test_plan_for_each_project_type(tmp_path):
    assert ids(build_plan(tmp_path, frozenset({"Cargo.toml"}))) == ["cargo-build"]
    assert ids(build_plan(tmp_path, frozenset({"pyproject.toml"}))) == ["py-venv", "py-install"]
    assert build_plan(tmp_path, frozenset({"requirements.txt"})).steps[1].argv == (f"{VENV}/bin/pip", "install", "-r", "requirements.txt")
    assert build_plan(tmp_path, frozenset({"setup.py"})).steps[1].argv == (f"{VENV}/bin/pip", "install", ".")
    assert ids(build_plan(tmp_path, frozenset({"go.mod"}))) == ["go-build"]
    assert ids(build_plan(tmp_path, frozenset({"CMakeLists.txt"}))) == ["cmake-config", "cmake-build"]
    assert ids(build_plan(tmp_path, frozenset({"meson.build"}))) == ["meson-setup", "meson-compile"]


def test_make_only_when_no_other_build_system(tmp_path):
    assert ids(build_plan(tmp_path, frozenset({"Makefile"}))) == ["make"]
    assert ids(build_plan(tmp_path, frozenset({"Makefile", "Cargo.toml"}))) == ["cargo-build"]


def test_docker_java_ruby_and_unknown_are_not_proposed(tmp_path):
    for f in ("Dockerfile", "pom.xml", "build.gradle", "Gemfile"):
        assert build_plan(tmp_path, frozenset({f})).steps == ()
    assert build_plan(tmp_path, frozenset()).steps == ()


def test_npm_build_step_only_when_package_json_has_a_build_script(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"build": "evil && rm -rf ~", "test": "x"}}))
    plan = build_plan(tmp_path)
    assert ids(plan) == ["npm-install", "npm-build"]
    assert plan.steps[1].argv == ("npm", "run", "build")  # the script text is never used
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "x"}}))
    assert ids(build_plan(tmp_path)) == ["npm-install"]
    for bad in ("not json", "[]", '{"scripts": []}', '{"scripts": {"build": 5}}'):
        (tmp_path / "package.json").write_text(bad)
        assert ids(build_plan(tmp_path)) == ["npm-install"]


def test_oversized_or_symlinked_package_json_gives_no_build_step(tmp_path):
    (tmp_path / "package.json").write_text('{"scripts": {"build": "x"}}' + " " * 300_000)
    assert ids(build_plan(tmp_path)) == ["npm-install"]
    real = tmp_path / "real.json"
    real.write_text('{"scripts": {"build": "x"}}')
    (tmp_path / "package.json").unlink()
    os.symlink(real, tmp_path / "package.json")
    assert ids(build_plan(tmp_path)) == []  # a symlinked build file is not trusted at all


def test_plan_commands_are_plain_argv_without_shell_syntax(tmp_path):
    every = frozenset({"Cargo.toml", "pyproject.toml", "package.json", "go.mod", "CMakeLists.txt", "meson.build"})
    for st in build_plan(tmp_path, every).steps:
        assert isinstance(st.argv, tuple) and all(isinstance(a, str) for a in st.argv)
        assert not any(c in a for a in st.argv for c in ";|&$`<>\n")


def test_digest_covers_folder_and_commands():
    a = Plan("/x", (Step("a", "A", ("cargo", "build")),))
    assert a.digest == Plan("/x", (Step("a", "A", ("cargo", "build")),)).digest
    assert a.digest != Plan("/y", a.steps).digest
    assert a.digest != Plan("/x", (Step("a", "A", ("cargo", "build", "--evil")),)).digest


def test_step_text_hides_the_venv_marker():
    assert Step("p", "P", (f"{VENV}/bin/pip", "install", ".")).text() == ".venv/bin/pip install ."


# ---------------------------------------------------------------- runner: refusals

def test_refuses_when_switched_off(tmp_path):
    r, plan, *_ = setup(tmp_path, enabled=False)
    with pytest.raises(RunError, match="turned off"):
        r.start("github", "o/r", "t", plan.digest)


def test_refuses_repository_that_is_not_cloned_or_invalid(tmp_path):
    r, plan, *_ = setup(tmp_path)
    with pytest.raises(RunError, match="not cloned"):
        r.start("github", "o/other", "t", plan.digest)
    with pytest.raises(RunError, match="invalid"):
        r.start("github", "../x", "t", plan.digest)
    with pytest.raises(RunError, match="invalid"):
        r.start("nope", "o/r", "t", plan.digest)


def test_refuses_stale_digest_and_unknown_step(tmp_path):
    r, plan, *_ = setup(tmp_path)
    with pytest.raises(RunError, match="changed"):
        r.start("github", "o/r", "t", "0" * 64)
    with pytest.raises(RunError, match="no such step"):
        r.start("github", "o/r", "evil-step", plan.digest)


def test_refuses_when_the_origin_no_longer_matches(tmp_path):
    r, plan, _, d = setup(tmp_path)
    (d / ".git" / "config").write_text('[remote "origin"]\n\turl = https://github.com/someone/else.git\n')
    with pytest.raises(RunError):  # the scan no longer maps this folder to o/r at all
        r.start("github", "o/r", "t", plan.digest)


def test_refuses_a_symlinked_clone_folder(tmp_path):
    d = make_clone(tmp_path)
    real = tmp_path / "real"
    d.rename(real)
    os.symlink(real, d)
    aw = Awareness(tmp_path / "clones", MACHINE, scan=lambda root: {"github:o/r": CloneInfo("github", "o/r", str(d))})
    st = Settings()
    st.set("guided_run", True)
    plan = Plan(str(d), (Step("t", "T", (PY, "-c", "pass")),))
    r = Runner(aw, st, plan_fn=lambda f: plan)
    with pytest.raises(RunError, match="plain folder"):
        r.start("github", "o/r", "t", plan.digest)


def test_refuses_a_missing_tool_and_a_tool_inside_the_repository(tmp_path):
    d = make_clone(tmp_path)
    plan = Plan(str(d), (Step("t", "T", ("cargo", "build")),))
    r, *_ = setup(tmp_path, plan=plan, which=lambda n: None)
    with pytest.raises(RunError, match="not found"):
        r.start("github", "o/r", "t", plan.digest)
    planted = d / "cargo"
    planted.write_text("#!/bin/sh\necho pwned\n")
    planted.chmod(0o755)
    r, *_ = setup(tmp_path, plan=plan, which=lambda n: str(planted))
    with pytest.raises(RunError, match="inside the repository"):
        r.start("github", "o/r", "t", plan.digest)
    r, *_ = setup(tmp_path, plan=plan, which=lambda n: "relative/cargo")
    with pytest.raises(RunError, match="not found"):
        r.start("github", "o/r", "t", plan.digest)


def test_venv_step_needs_an_existing_venv_inside_the_folder(tmp_path):
    d = make_clone(tmp_path)
    plan = Plan(str(d), (Step("p", "P", (f"{VENV}/bin/pip", "install", ".")),))
    r, *_ = setup(tmp_path, plan=plan)
    with pytest.raises(RunError, match="virtual environment"):
        r.start("github", "o/r", "p", plan.digest)
    (d / ".venv" / "bin").mkdir(parents=True)
    os.symlink("/bin/sh", d / ".venv" / "bin" / "pip")  # resolves OUTSIDE the venv
    with pytest.raises(RunError, match="virtual environment"):
        r.start("github", "o/r", "p", plan.digest)


# ---------------------------------------------------------------- runner: running

def test_runs_one_step_and_reports_success(tmp_path):
    r, plan, log, _ = setup(tmp_path, "print('hello there')")
    s = wait(r.start("github", "o/r", "t", plan.digest))
    assert (s.status, s.exit_code) == ("done", 0) and "hello there" in s.output
    e = log.recent()[0]
    assert (e.host, e.slug, e.action, e.ok) == ("github", "o/r", "run", True) and "hello" not in e.result


def test_failure_is_reported_with_exit_code(tmp_path):
    r, plan, log, _ = setup(tmp_path, "import sys; print('bad'); sys.exit(3)")
    s = wait(r.start("github", "o/r", "t", plan.digest))
    assert (s.status, s.exit_code) == ("failed", 3) and log.recent()[0].ok is False


def test_child_environment_is_minimal_and_has_no_secrets(tmp_path):
    env = {"PATH": os.environ["PATH"], "HOME": "/h", "GITHUB_TOKEN": "ghp_SECRET", "REPOHUB_X_TOKEN": "s2",
           "AWS_SESSION_TOKEN": "s3", "LANG": "C"}
    r, plan, *_ = setup(tmp_path, "import os; print(sorted(os.environ))", environ=env)
    out = wait(r.start("github", "o/r", "t", plan.digest)).output
    assert "GITHUB_TOKEN" not in out and "REPOHUB" not in out and "AWS" not in out and "'HOME'" in out


def test_popen_gets_argv_list_no_shell_own_group_no_stdin_and_the_clone_as_cwd(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(rn.os, "killpg", lambda *a: None)  # the fake process has no real group

    class FakeProc:
        pid = 999999
        stdout = io.BytesIO(b"")

        def poll(self):
            return 0

        def wait(self):
            return 0

    def fake_popen(argv, **kw):
        seen.update(argv=argv, **kw)
        return FakeProc()

    r, plan, _, d = setup(tmp_path, popen=fake_popen)
    wait(r.start("github", "o/r", "t", plan.digest))
    assert isinstance(seen["argv"], list) and seen["shell"] is False and seen["start_new_session"] is True
    assert seen["stdin"] == subprocess.DEVNULL and seen["cwd"] == str(d) and seen["close_fds"] is True


def test_ansi_and_control_characters_are_stripped_from_output(tmp_path):
    code = "print('\\x1b[31mred\\x1b[0m'); print('a\\rb'); print('x\\x1b]0;title\\x07y'); print('bidi\\u202eevil')"
    r, plan, *_ = setup(tmp_path, code)
    out = wait(r.start("github", "o/r", "t", plan.digest)).output
    assert "\x1b" not in out and "‮" not in out and "red" in out and "xy" in out and "a\nb" in out


def test_clean_output_directly():
    assert clean_output(b"\x1b[1mbold\x1b[0m\r\nline\x00\x07") == "bold\nline"


def test_output_is_capped_keeping_the_tail(tmp_path):
    r, plan, *_ = setup(tmp_path, "print('A'*5000); print('THE-END')", max_output=1000)
    s = wait(r.start("github", "o/r", "t", plan.digest))
    assert s.truncated and len(s.output) <= 1000 and "THE-END" in s.output


def test_only_one_command_at_a_time(tmp_path):
    r, plan, *_ = setup(tmp_path, "import time; time.sleep(2)")
    s = r.start("github", "o/r", "t", plan.digest)
    assert r.current() is s
    with pytest.raises(RunError, match="still running"):
        r.start("github", "o/r", "t", plan.digest)
    r.cancel(s.id)
    wait(s)
    assert r.current() is None
    wait(r.start("github", "o/r", "t", plan.digest))  # allowed again


def test_timeout_kills_the_command(tmp_path):
    r, plan, log, _ = setup(tmp_path, "import time; time.sleep(30)", timeout=0.3)
    t0 = time.monotonic()
    s = wait(r.start("github", "o/r", "t", plan.digest))
    assert s.status == "timed out" and time.monotonic() - t0 < 8 and log.recent()[0].ok is False


def test_cancel_kills_the_whole_process_group_including_grandchildren(tmp_path):
    marker = tmp_path / "grandchild.pid"
    code = ("import subprocess, sys, time\n"
            f"p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            f"open({str(marker)!r}, 'w').write(str(p.pid))\n"
            "time.sleep(60)\n")
    r, plan, *_ = setup(tmp_path, code)
    s = r.start("github", "o/r", "t", plan.digest)
    for _ in range(100):
        if marker.exists() and marker.read_text():
            break
        time.sleep(0.05)
    gpid = int(marker.read_text())
    assert r.cancel(s.id) is True
    wait(s)
    assert s.status == "cancelled"
    for _ in range(100):  # the grandchild dies with the group
        try:
            os.kill(gpid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        os.kill(gpid, 9)
        pytest.fail("grandchild survived")


def test_cancel_unknown_or_finished_session_is_false(tmp_path):
    r, plan, *_ = setup(tmp_path)
    assert r.cancel("nope") is False
    s = wait(r.start("github", "o/r", "t", plan.digest))
    assert r.cancel(s.id) is False


def test_no_signal_is_sent_after_the_leader_was_reaped(tmp_path, monkeypatch):
    sent = []
    real = os.killpg
    monkeypatch.setattr(rn.os, "killpg", lambda *a: (sent.append(a), real(*a)))
    r, plan, *_ = setup(tmp_path, "print('quick')")
    s = wait(r.start("github", "o/r", "t", plan.digest))
    before = len(sent)
    assert r.cancel(s.id) is False
    r._kill(s, hard=True)
    r._kill(s)
    assert len(sent) == before  # once reaped, its group id could belong to someone else: never signalled


def test_old_sessions_are_forgotten(tmp_path):
    r, plan, *_ = setup(tmp_path, "pass")
    for _ in range(rn.KEEP_SESSIONS + 3):
        wait(r.start("github", "o/r", "t", plan.digest))
    assert len(r._sessions) <= rn.KEEP_SESSIONS


# ---------------------------------------------------------------- web

TOKEN = "t0k"


def web(tmp_path, code="print('hi')", enabled=True, cloned=True):
    d = make_clone(tmp_path)
    if not cloned:
        (d / ".git" / "config").write_text("[core]\n")
    aw = Awareness(tmp_path / "clones", MACHINE)
    st = Settings()
    st.set("guided_run", enabled)
    plan = Plan(str(d), (Step("t", "Test step", (PY, "-c", code)),))
    runner = Runner(aw, st, ActionLog(), plan_fn=lambda f: plan, environ={"PATH": os.environ["PATH"]})
    gh = FakeProvider("github", [mk("github", "o/r", 5)])
    app = create_app(make_hub(gh), tmp_path / "clones", session_token=TOKEN, shelves=[Shelf("s", topic="x")],
                     awareness=aw, runner=runner)
    return TestClient(app, base_url="http://localhost"), runner, plan


def post(c, url, **data):
    return c.post(url, data={"token": TOKEN, **data}, follow_redirects=False)


def test_plan_page_states(tmp_path):
    c, _, plan = web(tmp_path, enabled=False)
    t = c.get("/run/github/o/r").text
    assert "Guided install is <strong>off</strong>" in t and "Approve and run" not in t and "cannot sandbox" in t
    c, _, plan = web(tmp_path, cloned=False)
    assert "not cloned" in c.get("/run/github/o/r").text
    c, _, plan = web(tmp_path)
    t = c.get("/run/github/o/r").text
    assert "Approve and run this command" in t and plan.digest in t and "-c" in t


def test_get_never_runs_anything(tmp_path):
    c, runner, _ = web(tmp_path)
    for url in ("/run/github/o/r", "/runs/whatever", "/runs/whatever/output"):
        c.get(url)
    assert runner.current() is None and not runner._sessions


def test_switch_needs_token_and_valid_action(tmp_path):
    c, runner, _ = web(tmp_path, enabled=False)
    assert c.post("/runsetting", data={"host": "github", "slug": "o/r", "action": "on"}).status_code == 403
    assert post(c, "/runsetting", host="github", slug="o/r", action="explode").status_code == 404
    assert runner.enabled is False
    assert post(c, "/runsetting", host="github", slug="o/r", action="on").headers["location"] == "/run/github/o/r"
    assert runner.enabled is True


def test_start_needs_token_and_the_matching_digest(tmp_path):
    c, runner, plan = web(tmp_path)
    data = {"host": "github", "slug": "o/r", "step": "t", "digest": plan.digest}
    assert c.post("/runstart", data=data).status_code == 403
    assert c.post("/runstart", data={**data, "token": "bad"}).status_code == 403
    assert post(c, "/runstart", **{**data, "digest": "f" * 64}).status_code == 409
    assert post(c, "/runstart", **{**data, "step": "rm-rf"}).status_code == 409
    assert not runner._sessions


def test_start_run_watch_output_and_finish(tmp_path):
    c, runner, plan = web(tmp_path, "print('<script>alert(1)</script>')")
    r = post(c, "/runstart", host="github", slug="o/r", step="t", digest=plan.digest)
    assert r.status_code == 303 and r.headers["location"].startswith("/runs/")
    sid = r.headers["location"].split("/")[-1]
    wait(runner.get(sid))
    page_ = c.get(f"/runs/{sid}").text
    assert "hx-get" in page_
    out = c.get(f"/runs/{sid}/output").text
    assert "Done" in out and "exit code 0" in out
    assert "<script>alert(1)</script>" not in out and "&lt;script&gt;" in out
    assert 'hx-trigger="every 1s"' not in out  # polling stops when finished


def test_running_output_polls_and_offers_cancel(tmp_path):
    c, runner, plan = web(tmp_path, "import time; time.sleep(3)")
    sid = post(c, "/runstart", host="github", slug="o/r", step="t", digest=plan.digest).headers["location"].split("/")[-1]
    out = c.get(f"/runs/{sid}/output").text
    assert 'hx-trigger="every 1s"' in out and "Cancel" in out
    assert c.post(f"/runs/{sid}/cancel").status_code == 403
    assert post(c, f"/runs/{sid}/cancel").status_code == 303
    wait(runner.get(sid))
    assert "Cancelled" in c.get(f"/runs/{sid}/output").text


def test_second_start_while_running_is_refused(tmp_path):
    c, runner, plan = web(tmp_path, "import time; time.sleep(3)")
    data = dict(host="github", slug="o/r", step="t", digest=plan.digest)
    sid = post(c, "/runstart", **data).headers["location"].split("/")[-1]
    r = post(c, "/runstart", **data)
    assert r.status_code == 409 and "still running" in r.text
    runner.cancel(sid)
    wait(runner.get(sid))


def test_start_refused_when_switched_off_even_with_a_valid_digest(tmp_path):
    c, runner, plan = web(tmp_path, enabled=False)
    r = post(c, "/runstart", host="github", slug="o/r", step="t", digest=plan.digest)
    assert r.status_code == 409 and "turned off" in r.text and not runner._sessions


def test_unknown_session_is_404_and_repo_page_link_needs_a_clone(tmp_path):
    c, *_ = web(tmp_path)
    assert c.get("/runs/nope").status_code == 404 and c.get("/runs/nope/output").status_code == 404
    assert post(c, "/runs/nope/cancel").status_code == 404
    assert "Install / build…" in c.get("/repo/github/o/r").text
    c2, *_ = web(tmp_path, cloned=False)
    assert "Install / build…" not in c2.get("/repo/github/o/r").text


# ---------------------------------------------------------------- CLI

def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(argv, hub_factory=lambda: pytest.fail("no hub"), stdout=out, stderr=err, gh_cli=lambda: None)
    return code, out.getvalue(), err.getvalue()


def test_cli_plan_prints_but_never_runs(tmp_path, monkeypatch):
    d = make_clone(tmp_path)
    (d / "Cargo.toml").write_text("")
    monkeypatch.setenv("REPOHUB_CLONE_DIR", str(tmp_path / "clones"))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("the CLI must never run anything"))
    code, out, err = run_cli(["plan", "github:o/r", "--json"])
    doc = json.loads(out)
    assert code == 0 and doc["steps"] == [{"id": "cargo-build", "title": "Build with cargo (release)",
                                           "command": "cargo build --release"}] and "sandbox" in doc["warning"]
    code, out, err = run_cli(["plan", "github:o/r"])
    assert "1. Build with cargo" in out and "proposal only" in err


def test_cli_plan_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("REPOHUB_CLONE_DIR", str(tmp_path / "clones"))
    assert run_cli(["plan", "nonsense"])[0] == 2
    assert run_cli(["plan", "github:o/none"])[0] == 1


def test_cli_has_no_run_command():
    for cmd in ("run", "install", "build"):
        with pytest.raises(SystemExit):
            cli.main([cmd, "github:o/r"], stdout=io.StringIO(), stderr=io.StringIO())


# ---------------------------------------------------------------- terminal app

async def test_tui_install_flow_needs_approval_and_runs_once(tmp_path):
    from textual.widgets import DataTable, Static

    from repohub.tui.app import ConfirmWrite, DetailScreen, RepoHubApp, RunScreen

    d = make_clone(tmp_path)
    aw = Awareness(tmp_path / "clones", MACHINE)
    st = Settings()
    st.set("guided_run", True)
    plan = Plan(str(d), (Step("t", "Test step", (PY, "-c", "print('from the step')")),))
    runner = Runner(aw, st, ActionLog(), plan_fn=lambda f: plan, environ={"PATH": os.environ["PATH"]})
    gh = FakeProvider("github", [mk("github", "o/r", 5)])
    app = RepoHubApp(make_hub(gh), tmp_path / "clones", shelves=[Shelf("s", topic="x")], awareness=aw, runner=runner)
    async with app.run_test() as pilot:
        app.push_screen(DetailScreen(app.hub, "github", "o/r", tmp_path, app.cloner))
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("i")
        await pilot.pause()
        assert isinstance(app.screen, RunScreen)
        app.screen.query_one(DataTable).focus()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmWrite) and "-c" in app.screen.text and "sandbox" in app.screen.text
        assert not runner._sessions  # nothing runs before y
        await pilot.press("n")
        await pilot.pause()
        assert not runner._sessions
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        (s,) = runner._sessions.values()
        wait(s)
        await pilot.pause(0.7)
        assert "from the step" in str(app.screen.query_one("#log", Static).render())


async def test_tui_install_screen_when_off_or_not_cloned(tmp_path):
    from textual.widgets import Static

    from repohub.tui.app import RepoHubApp, RunScreen

    make_clone(tmp_path)
    aw = Awareness(tmp_path / "clones", MACHINE)
    runner = Runner(aw, Settings(), ActionLog(), environ={})
    app = RepoHubApp(make_hub(FakeProvider("github", [])), tmp_path / "clones", shelves=[Shelf("s", topic="x")], awareness=aw, runner=runner)
    async with app.run_test() as pilot:
        app.push_screen(RunScreen(runner, "github", "o/r"))
        await pilot.pause()
        assert "off" in str(app.screen.query_one("#info", Static).render()).lower()
        app.pop_screen()
        app.push_screen(RunScreen(runner, "github", "o/none"))
        await pilot.pause()
        assert "not cloned" in str(app.screen.query_one("#info", Static).render())


async def test_tui_f6_toggles_guided_install_with_confirmation(tmp_path):
    from repohub.tui.app import ConfirmWrite, RepoHubApp

    runner = Runner(Awareness(tmp_path, MACHINE), Settings(), ActionLog(), environ={})
    app = RepoHubApp(make_hub(FakeProvider("github", [])), tmp_path, shelves=[Shelf("s", topic="x")],
                     awareness=Awareness(tmp_path, MACHINE), runner=runner)
    async with app.run_test() as pilot:
        await pilot.press("f6")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmWrite) and not runner.enabled
        await pilot.press("n")
        await pilot.pause()
        assert not runner.enabled
        await pilot.press("f6")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        assert runner.enabled
        await pilot.press("f6")  # turning it off never needs a prompt
        await pilot.pause()
        assert not runner.enabled


# ---------------------------------------------------------------- review follow-ups

def test_a_leftover_background_process_cannot_wedge_the_runner(tmp_path, monkeypatch):
    monkeypatch.setattr(rn, "DRAIN_GRACE", 0.3)
    marker = tmp_path / "bg.pid"
    code = ("import subprocess, sys\n"
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            f"open({str(marker)!r}, 'w').write(str(p.pid))\n")  # the step ends, its child keeps the output pipe open
    r, plan, *_ = setup(tmp_path, code)
    t0 = time.monotonic()
    s = wait(r.start("github", "o/r", "t", plan.digest))
    assert time.monotonic() - t0 < 6 and s.status == "done" and r.current() is None
    gpid = int(marker.read_text())
    for _ in range(100):  # the straggler was killed with the group
        try:
            os.kill(gpid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        os.kill(gpid, 9)
        pytest.fail("background process survived")
    wait(r.start("github", "o/r", "t", plan.digest))  # the runner is usable again


def test_a_repository_cannot_supply_its_own_venv_pip(tmp_path):
    d = make_clone(tmp_path)
    plan = Plan(str(d), (Step("py-venv", "V", (PY, "-c", "pass")), Step("py-install", "P", (f"{VENV}/bin/pip", "install", "."))))
    (d / ".venv" / "bin").mkdir(parents=True)
    pip = d / ".venv" / "bin" / "pip"
    pip.write_text("#!/bin/sh\necho planted\n")
    pip.chmod(0o755)
    r, *_ = setup(tmp_path, plan=plan)
    with pytest.raises(RunError, match="Create a virtual environment"):
        r.start("github", "o/r", "py-install", plan.digest)  # the step 1 has not been run by RepoHub
    wait(r.start("github", "o/r", "py-venv", plan.digest))  # once step 1 has run here, the venv counts
    s = wait(r.start("github", "o/r", "py-install", plan.digest))
    assert s.exit_code == 0 and "planted" in s.output  # (the fake plan reuses the tmp folder's own pip on purpose)


def test_a_symlinked_venv_is_refused(tmp_path):
    d = make_clone(tmp_path)
    plan = Plan(str(d), (Step("py-venv", "V", (PY, "-c", "pass")),))
    os.symlink("/usr", d / ".venv")
    r, *_ = setup(tmp_path, plan=plan)
    with pytest.raises(RunError, match="symlink"):
        r.start("github", "o/r", "py-venv", plan.digest)


def test_child_path_drops_relative_empty_and_repository_entries(tmp_path):
    d = make_clone(tmp_path)
    (d / "bin").mkdir()
    env = {"PATH": os.pathsep.join(["", ".", "relative/bin", str(d / "bin"), str(d), "/usr/bin", "/bin"])}
    r, *_ = setup(tmp_path, environ=env)
    assert r._child_env(d)["PATH"] == "/usr/bin:/bin"
    r2, *_ = setup(tmp_path, environ={"PATH": "."})
    assert r2._child_env(d)["PATH"] == rn.SAFE_PATH


def test_the_child_really_runs_with_the_filtered_path(tmp_path):
    d = make_clone(tmp_path)
    env = {"PATH": os.pathsep.join([".", str(d), os.path.dirname(PY)])}
    r, plan, *_ = setup(tmp_path, "import os; print(os.environ['PATH'])", environ=env)
    out = wait(r.start("github", "o/r", "t", plan.digest)).output
    assert str(d) not in out and os.path.dirname(PY) in out


def test_the_build_script_text_is_displayed_and_changes_the_digest(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"build": "node make.js \u202e && curl evil"}}))
    plan = build_plan(tmp_path)
    build = plan.step("npm-build")
    assert "node make.js" in build.note and "\u202e" not in build.note and build.argv == ("npm", "run", "build")
    assert "install scripts" in plan.step("npm-install").note
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"build": "something else"}}))
    assert build_plan(tmp_path).digest != plan.digest  # changed script text invalidates a stale approval


def test_long_script_text_is_truncated(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"build": "x" * 5000}}))
    assert len(build_plan(tmp_path).step("npm-build").note) < 400


def test_web_plan_page_shows_the_notes_escaped(tmp_path):
    d = make_clone(tmp_path)
    aw = Awareness(tmp_path / "clones", MACHINE)
    st = Settings()
    st.set("guided_run", True)
    plan = Plan(str(d), (Step("t", "T", (PY, "-c", "pass"), "<img src=x onerror=alert(1)>"),))
    runner = Runner(aw, st, ActionLog(), plan_fn=lambda f: plan, environ={})
    app = create_app(make_hub(FakeProvider("github", [])), tmp_path / "clones", session_token=TOKEN,
                     shelves=[Shelf("s", topic="x")], awareness=aw, runner=runner)
    t = TestClient(app, base_url="http://localhost").get("/run/github/o/r").text
    assert "<img src=x" not in t and "&lt;img" in t


def test_shutdown_stops_running_commands(tmp_path):
    r, plan, *_ = setup(tmp_path, "import time; time.sleep(60)")
    s = r.start("github", "o/r", "t", plan.digest)
    time.sleep(0.3)
    r.shutdown()
    wait(s)
    assert s.status == "cancelled"
