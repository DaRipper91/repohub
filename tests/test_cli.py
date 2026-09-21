import io
import json
import os
import re

import pytest

import repohub.cli as cli
from helpers import FakeProvider, make_hub, mk
from repohub.core.browse import LoadedShelves, Shelf, ShelfEntry, Snapshot
from repohub.core.models import Asset, Release
from repohub.core.providers.base import NotFound, ProviderError

TOKEN = "ghp_FAKETOKEN1234567890"
CONTROL = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def run(argv, hub=None, **kw):
    out, err = io.StringIO(), io.StringIO()

    def factory():
        if hub is None:
            raise AssertionError("hub_factory must not be called")
        return hub

    kw.setdefault("gh_cli", lambda: None)  # never run the real gh CLI
    code = cli.main(argv, hub_factory=factory, stdout=out, stderr=err, **kw)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture(autouse=True)
def config_dir(monkeypatch, tmp_path):
    """Never read the real user config dir (hosts.yaml)."""
    cfg = tmp_path / "xdg-config"
    cfg.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg))
    return cfg


@pytest.fixture(autouse=True)
def token_env(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    monkeypatch.setenv("GITLAB_TOKEN", TOKEN)


@pytest.fixture
def shelves(monkeypatch):
    curated = Shelf("Curated One", repos=(
        ShelfEntry("github", "o/r", "nice one", Snapshot("snap desc", 5, "Go", "MIT", "2026-01-02")),
        ShelfEntry("github", "x/y", "second")), as_of="2026-09-01")
    lst = [Shelf("Rust TUIs", query="tui", language="Rust"), curated]
    problems = ["bad file"]
    monkeypatch.setattr(cli, "load_all_shelves", lambda *a, **k: LoadedShelves(list(lst), list(problems)))
    return lst


def gh(repos=None, **kw):
    return FakeProvider("github", repos if repos is not None else [mk(stars=10)], **kw)


def test_search_json_schema():
    hub = make_hub(gh(), FakeProvider("gitlab"))
    code, out, err = run(["search", "tui", "--json"], hub)
    doc = json.loads(out)
    assert code == 0 and err == ""
    assert doc["schema_version"] == 1 and doc["errors"] == {} and doc["stale"] is False
    assert set(doc["repos"][0]) == set(mk().to_dict())


def test_flags_map_to_filters():
    g = gh()
    lab = FakeProvider("gitlab")
    hub = make_hub(g, lab)
    code, _, _ = run(["search", "tui", "app", "--lang", "Rust", "--min-stars", "50", "--days", "30",
                      "--host", "github", "--sort", "updated", "--no-forks", "--archived"], hub)
    f = g.last_filters
    assert code == 0 and g.last_query == "tui app"
    assert (f.language, f.min_stars, f.updated_within_days, f.hosts, f.sort, f.hide_forks,
            f.include_archived) == ("Rust", 50, 30, ("github",), "updated", True, True)
    assert lab.calls == 0


def test_query_syntax_and_problems_to_stderr():
    g = gh()
    hub = make_hub(g)
    code, out, err = run(["search", "tui", "lang:go", "stars:>7", "days:abc", "--host", "github"], hub)
    assert g.last_filters.language == "go"
    assert g.last_filters.min_stars == 7 and g.last_query == "tui"
    assert "Ignored: days needs a whole number" in err
    assert "Ignored" not in out


def test_search_limit():
    repos = [mk(slug=f"o/r{i}", stars=100 - i) for i in range(5)]
    code, out, _ = run(["search", "x", "--limit", "2", "--json"], make_hub(gh(repos)))
    assert len(json.loads(out)["repos"]) == 2


def test_limit_out_of_range_is_usage_error():
    with pytest.raises(SystemExit) as e:
        run(["search", "x", "--limit", "0"])
    assert e.value.code == 2


def test_partial_and_total_failure():
    hub = make_hub(gh(), FakeProvider("gitlab", error=ProviderError("gitlab", "boom")))
    code, out, err = run(["search", "tui", "--json"], hub)
    assert code == 3 and json.loads(out)["errors"] == {"gitlab": "boom"}
    hub = make_hub(gh(error=ProviderError("github", "down")), FakeProvider("gitlab", error=ProviderError("gitlab", "boom")))
    code, out, err = run(["search", "tui", "--json"], hub)
    assert code == 1 and json.loads(out)["repos"] == []


REL = Release("v1", "2026-09-01T00:00:00Z", (Asset("a-arm64.tar.gz", 2_000_000, "https://x/a", "arm64"),
                                            Asset("a-x64.zip", 10, "https://x/b", "x86_64")))


def test_repo_json_release_and_readme():
    hub = make_hub(gh(release=REL, readme="# hello"))
    code, out, err = run(["repo", "github:o/r", "--json"], hub)
    doc = json.loads(out)
    assert code == 0 and doc["schema_version"] == 1 and doc["readme"] is None
    assert [a["arch"] for a in doc["release"]["assets"]] == ["arm64", "x86_64"]
    assert doc["repo"]["slug"] == "o/r"
    code, out, _ = run(["repo", "github:o/r", "--json", "--readme"], make_hub(gh(release=REL, readme="# hello")))
    assert json.loads(out)["readme"] == "# hello"


def test_repo_human_marks_arm64():
    code, out, _ = run(["repo", "github:o/r", "--readme"], make_hub(gh(release=REL, readme="# hello")))
    assert "a-arm64.tar.gz (arm64)" in out and "(arm64)" not in out.split("a-x64.zip")[1].split("\n")[0]
    assert "# hello" in out


def test_repo_not_found():
    hub = make_hub(gh(detail_error=NotFound("github", "no such repo")))
    code, out, err = run(["repo", "github:o/r", "--json"], hub)
    assert code == 1 and out == "" and "not found" in err


@pytest.mark.parametrize("arg", ["../x", "nohost:o/r", "github:o", "o/r", "github:../x"])
def test_repo_invalid_argument_before_hub(arg):
    code, out, err = run(["repo", arg])  # a factory call would raise AssertionError
    assert code == 2 and out == "" and err


def test_shelves_json(shelves):
    code, out, err = run(["shelves", "--json"])
    doc = json.loads(out)
    assert code == 0 and doc["schema_version"] == 1
    assert doc["shelves"] == [{"index": 0, "name": "Rust TUIs", "kind": "search", "entries": 0},
                              {"index": 1, "name": "Curated One", "kind": "curated", "entries": 2}]
    assert "bad file" in err


def test_shelf_by_name_and_index(shelves):
    g = gh()
    hub = make_hub(g)
    code, out, _ = run(["shelf", "rust tuis", "--json"], hub)
    doc = json.loads(out)
    assert code == 0 and doc["shelf"] == "Rust TUIs" and g.last_query == "tui" and g.last_filters.language == "Rust"
    g2 = gh()
    code, out, _ = run(["shelf", "0", "--json"], make_hub(g2))
    assert code == 0 and g2.last_query == "tui"


def test_unknown_shelf(shelves):
    code, out, err = run(["shelf", "nope", "--json"], make_hub(gh()))
    assert code == 1 and out == "" and "no such shelf" in err
    assert run(["shelf", "9"], make_hub(gh()))[0] == 1


def test_curated_shelf_json(shelves):
    g = gh([mk(slug="o/r", stars=99)])
    code, out, _ = run(["shelf", "1", "--json"], make_hub(g))
    doc = json.loads(out)
    assert code == 3  # x/y could not be refreshed (fake provider has no such repo)
    first, second = doc["repos"]
    assert first["note"] == "nice one" and first["as_of"] == "2026-09-01" and first["live"] is True
    assert first["stars"] == 99 and second["live"] is False and second["note"] == "second"
    assert doc["errors"] and doc["shelf"] == "Curated One"


def test_curated_no_refresh_makes_no_provider_calls(shelves):
    g = gh()
    code, out, err = run(["shelf", "Curated One", "--no-refresh", "--json", "--limit", "1"], make_hub(g))
    doc = json.loads(out)
    assert code == 0 and g.calls == 0 and len(doc["repos"]) == 1
    assert doc["repos"][0]["live"] is False and doc["repos"][0]["stars"] == 5 and doc["errors"] == {}


def test_favorites_no_provider_calls():
    g = gh()
    hub = make_hub(g)
    hub.favorites.add(mk(slug="f/one"))
    code, out, err = run(["favorites", "--json"], hub)
    doc = json.loads(out)
    assert code == 0 and g.calls == 0 and [r["slug"] for r in doc["repos"]] == ["f/one"]
    assert doc["errors"] == {} and doc["stale"] is False and doc["schema_version"] == 1


def test_human_table_and_hostile_data():
    bad = mk(slug="o/evil", description="\x1b[31mred\x1b[0m\x07\u202e\u2028\x85 " + "long " * 30,
             language="G\x00o")
    code, out, err = run(["search", "x"], make_hub(gh([bad])))
    assert "o/evil" in out and "2026-09-10" in out and "..." in out
    assert not CONTROL.search(out) and "\x1b" not in out
    assert not re.search("[\u202a-\u202e\u2066-\u2069\u200e\u200f\u061c\u2028\u2029\x80-\x9f]", out)
    assert all(len(line) < 200 for line in out.splitlines())


def test_hostile_problem_and_error_text_is_clean():
    hub = make_hub(gh(error=ProviderError("github", "bad\x1b[2Jthing")))
    code, out, err = run(["search", "lang:\x1b[31m", "x"], hub)
    assert "\x1b" not in err and "\x1b" not in out


def test_json_mode_stdout_is_single_document():
    code, out, err = run(["search", "tui", "days:zzz", "--json"], make_hub(gh()))
    json.loads(out)  # whole stdout parses
    assert "Ignored" in err and "Ignored" not in out


def test_no_token_in_any_output(shelves):
    hub = make_hub(gh(release=REL))
    for argv in (["search", "tui"], ["search", "tui", "--json"], ["repo", "github:o/r", "--readme"],
                 ["shelves"], ["shelf", "0"], ["favorites"], ["repo", "bad"]):
        code, out, err = run(argv, hub)
        assert TOKEN not in out and TOKEN not in err
    assert TOKEN in os.environ.values()


def test_help_does_not_build_hub(capsys):
    for argv in (["--help"], ["search", "--help"], ["--version"]):
        out, err = io.StringIO(), io.StringIO()
        with pytest.raises(SystemExit) as e:
            cli.main(argv, hub_factory=lambda: (_ for _ in ()).throw(AssertionError("built")), stdout=out, stderr=err)
        assert e.value.code == 0
    assert "usage" in out.getvalue() or "repohub" in out.getvalue()


def test_unknown_subcommand_and_no_command():
    for argv in (["frobnicate"], []):
        err = io.StringIO()
        with pytest.raises(SystemExit) as e:
            cli.main(argv, hub_factory=lambda: None, stdout=io.StringIO(), stderr=err)
        assert e.value.code == 2 and err.getvalue()


def test_unexpected_exception_is_one_line():
    # search_all turns provider exceptions into "unexpected error"; force a hub-level failure instead
    hub = make_hub(gh())

    async def broken(*a, **k):
        raise RuntimeError(TOKEN)

    hub.search = broken
    code, out, err = run(["search", "x"], hub)
    assert code == 1 and err.strip() == "error: RuntimeError" and "Traceback" not in err and TOKEN not in err


def test_keyboard_interrupt_returns_130():
    def factory():
        raise KeyboardInterrupt

    assert cli.main(["favorites"], hub_factory=factory, stdout=io.StringIO(), stderr=io.StringIO()) == 130


RAW_BAD = "[\u2028\u2029\x85\x7f\u202a-\u202e\u2066-\u2069\u200e\u200f\u061c\x80-\x9f]"


def test_json_escapes_dangerous_characters():
    desc = "a\u2028b\x85c\x7fd\u202ee\u200ef\u2029g\u061ch é 日本語"
    code, out, err = run(["search", "x", "--json"], make_hub(gh([mk(description=desc)])))
    assert not re.search(RAW_BAD, out)
    assert "é" in out and "日本語" in out
    assert json.loads(out)["repos"][0]["description"] == desc


def test_table_cell_widths_are_capped():
    big = mk(slug="o/" + "s" * 5000, language="L" * 5000, host="github")
    code, out, err = run(["search", "x"], make_hub(gh([big])))
    lines = out.splitlines()
    assert all(len(line) < 200 for line in lines)
    row = lines[1]
    assert "s" * 61 not in row and "L" * 21 not in row and "…" in row
    hb = mk(slug="o/r2", host="gitlab" + "z" * 100)
    code, out, err = run(["search", "x"], make_hub(FakeProvider("gitlab", [hb])))
    assert len(out.splitlines()[1].split()[1]) <= 8


def _curated_run(g):
    shelf = Shelf("C", repos=(ShelfEntry("github", "o/r"), ShelfEntry("github", "x/y")))
    import repohub.cli as c
    orig = c.load_all_shelves
    c.load_all_shelves = lambda *a, **k: LoadedShelves([shelf], [])
    try:
        return run(["shelf", "C", "--json"], make_hub(g))
    finally:
        c.load_all_shelves = orig


def test_curated_all_refreshes_fail_exit_1():
    code, out, err = _curated_run(gh(detail_error=ProviderError("github", "down")))
    doc = json.loads(out)
    assert code == 1 and doc["errors"] and not any(r["live"] for r in doc["repos"])


def test_curated_some_refreshes_succeed_exit_3():
    code, out, err = _curated_run(gh([mk(slug="o/r")]))
    doc = json.loads(out)
    assert code == 3 and [r["live"] for r in doc["repos"]] == [True, False]


def test_all_parser_problems_are_printed_including_the_more_marker():
    g = gh()
    tokens = [f"days:x{i}" for i in range(15)]
    code, out, err = run(["search", "tui", *tokens], make_hub(g))
    lines = [ln for ln in err.splitlines() if ln.startswith("Ignored:")]
    assert len(lines) == 11 and lines[-1] == "Ignored: ... and more problems ignored"


def test_digits_only_shelf_argument_index_then_name(monkeypatch):
    lst = [Shelf("2", query="first"), Shelf("Other", query="o"), Shelf("Other", query="dup")]
    monkeypatch.setattr(cli, "load_all_shelves", lambda *a, **k: LoadedShelves(list(lst), []))
    g = gh()
    assert run(["shelf", "1", "--json"], make_hub(g))[0] == 0 and g.last_query == "o"  # valid index wins
    g = gh()
    assert run(["shelf", "2", "--json"], make_hub(g))[0] == 0 and g.last_query == "dup"  # valid index beats name "2"
    g = gh()
    assert run(["shelf", "2", "--json"], make_hub(g))[0] == 0
    lst[2] = Shelf("Third", query="t")
    g = gh()
    assert run(["shelf", "2", "--json"], make_hub(g))[0] == 0 and g.last_query == "t"
    lst[:] = [Shelf("Odd", query="x"), Shelf("5", query="named five")]
    g = gh()
    assert run(["shelf", "5", "--json"], make_hub(g))[0] == 0 and g.last_query == "named five"  # not an index: name
    lst[:] = [Shelf("2", query="first"), Shelf("Other", query="o"), Shelf("Other", query="dup")]
    g = gh()
    assert run(["shelf", "OTHER", "--json"], make_hub(g))[0] == 0 and g.last_query == "o"  # first duplicate wins
    assert run(["shelf", "7"], make_hub(gh()))[0] == 1


def test_search_limit_help_mentions_per_host_cap(capsys):
    with pytest.raises(SystemExit):
        cli.main(["search", "--help"])
    assert "30 results per request" in " ".join(capsys.readouterr().out.split())


# ---- Phase 2: every registered host ------------------------------------------------------------

HOSTILE = "bad\x1b[31m \x9b \u202e [/] [@click=app.quit]x[/]"


def write_hosts(cfg, text):
    d = cfg / "repohub"
    d.mkdir(exist_ok=True)
    (d / "hosts.yaml").write_text(text, encoding="utf-8")


def test_host_codeberg_maps_to_filters():
    cb = FakeProvider("codeberg", [mk("codeberg", "o/r")])
    g = gh()
    code, out, err = run(["search", "tui", "--host", "codeberg"], make_hub(g, cb))
    assert code == 0 and cb.last_filters.hosts == ("codeberg",) and g.calls == 0


def test_default_and_both_and_all_follow_registry():
    from repohub.core.models import SearchFilters

    for extra in ([], ["--host", "both"], ["--host", "all"]):
        g = gh()
        run(["search", "tui", *extra], make_hub(g))
        assert g.last_filters.hosts == SearchFilters().hosts == ("github", "gitlab", "codeberg")


def test_unknown_host_flag_exits_2_without_echo(capsys):
    with pytest.raises(SystemExit) as e:
        run(["search", "tui", "--host", "nowhere\x1b[31m"])
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "\x1b" not in err


def test_extra_host_is_accepted_by_host_flag(config_dir):
    write_hosts(config_dir, "- {id: myforge, kind: forgejo, url: https://git.example.org}\n")
    mf = FakeProvider("myforge", [mk("myforge", "o/r")])
    code, _, _ = run(["search", "tui", "--host", "myforge"], make_hub(mf))
    assert code == 0 and mf.last_filters.hosts == ("myforge",)


def test_repo_codeberg_json_with_fake_hub():
    hub = make_hub(FakeProvider("codeberg", [mk("codeberg", "o/r")]))
    code, out, err = run(["repo", "codeberg:o/r", "--json"], hub)
    doc = json.loads(out)
    assert code == 0 and err == "" and doc["repo"]["host"] == "codeberg"


@pytest.mark.parametrize("arg", ["nowhere:o/r", "codeberg:o/r/x", "codeberg:o", "nowhere\x1b[31m:o/r"])
def test_repo_bad_host_or_slug_exits_2_before_hub(arg):
    code, out, err = run(["repo", arg])  # the factory raises AssertionError if called
    assert code == 2 and out == "" and err and "\x1b" not in err


def test_repo_extra_host_from_file(config_dir):
    write_hosts(config_dir, "- {id: myforge, kind: forgejo, url: https://git.example.org}\n")
    hub = make_hub(FakeProvider("myforge", [mk("myforge", "o/r")]))
    code, out, _ = run(["repo", "myforge:o/r", "--json"], hub)
    assert code == 0 and json.loads(out)["repo"]["host"] == "myforge"


def test_hosts_json_shape_and_tokens_are_booleans(monkeypatch):
    monkeypatch.setenv("CODEBERG_TOKEN", TOKEN)
    code, out, err = run(["hosts", "--json"])  # no hub, no network
    doc = json.loads(out)
    assert code == 0 and doc["schema_version"] == 1 and doc["problems"] == []
    by_id = {h["id"]: h for h in doc["hosts"]}
    assert list(by_id) == ["github", "gitlab", "codeberg"]
    for h in by_id.values():
        assert set(h) == {"id", "kind", "name", "domain", "builtin", "token", "token_env"}
        assert h["builtin"] is True and isinstance(h["token"], bool)
    assert by_id["codeberg"]["kind"] == "forgejo" and by_id["codeberg"]["domain"] == "codeberg.org"
    assert by_id["codeberg"]["token"] is True and by_id["github"]["token"] is True
    assert by_id["codeberg"]["token_env"] == ["CODEBERG_TOKEN"]
    assert TOKEN not in out and TOKEN not in err


def test_hosts_token_false_when_absent(monkeypatch):
    for v in ("GITHUB_TOKEN", "GH_TOKEN", "GITLAB_TOKEN", "CODEBERG_TOKEN"):
        monkeypatch.delenv(v, raising=False)
    code, out, _ = run(["hosts", "--json"])
    assert [h["token"] for h in json.loads(out)["hosts"]] == [False, False, False]


def test_hosts_uses_injected_gh_cli_fallback(monkeypatch):
    for v in ("GITHUB_TOKEN", "GH_TOKEN"):
        monkeypatch.delenv(v, raising=False)
    code, out, err = run(["hosts", "--json"], gh_cli=lambda: "ghp_FROMCLI_SECRET")
    assert json.loads(out)["hosts"][0]["token"] is True
    assert "ghp_FROMCLI_SECRET" not in out + err


def test_hosts_extra_host_not_builtin(config_dir, monkeypatch):
    write_hosts(config_dir, "- {id: myforge, kind: forgejo, url: https://git.example.org}\n")
    monkeypatch.setenv("REPOHUB_MYFORGE_TOKEN", "sekrit-forge-value")
    code, out, err = run(["hosts", "--json"])
    doc = json.loads(out)
    mf = next(h for h in doc["hosts"] if h["id"] == "myforge")
    assert mf["builtin"] is False and mf["kind"] == "forgejo" and mf["domain"] == "git.example.org"
    assert mf["token"] is True and mf["token_env"] == ["REPOHUB_MYFORGE_TOKEN"]
    assert "sekrit-forge-value" not in out + err
    code, out, err = run(["hosts"])
    assert "myforge" in out and "sekrit-forge-value" not in out + err


def test_hosts_broken_file_warns_on_stderr_and_lists_builtins(config_dir):
    write_hosts(config_dir, "- {id: [unclosed\n")
    code, out, err = run(["hosts", "--json"])
    doc = json.loads(out)
    assert code == 0 and [h["id"] for h in doc["hosts"]] == ["github", "gitlab", "codeberg"]
    assert doc["problems"] and err.startswith("warning: ")
    code, out, err = run(["hosts"])
    assert "codeberg" in out and err.startswith("warning: ")


def test_hosts_hostile_problem_text_is_sanitised(monkeypatch):
    monkeypatch.setattr(cli, "configure_hosts", lambda *a, **k: [HOSTILE])
    code, out, err = run(["hosts", "--json"])
    assert code == 0
    for text in (out, err):
        assert not CONTROL.search(text.replace("\n", "")) and "\u202e" not in text
    assert json.loads(out)["problems"]
    assert err.startswith("warning: ")


def test_hosts_needs_no_hub_and_help_reads_no_file(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(cli, "configure_hosts", lambda *a, **k: calls.append(1) or [])
    for argv in (["--help"], ["--version"], ["hosts", "--help"], ["search", "--help"], ["search", "x", "--limit", "0"]):
        with pytest.raises(SystemExit):
            run(argv)
    assert calls == []
    capsys.readouterr()


def test_tokens_from_env_never_reach_any_output(monkeypatch, shelves, config_dir):
    write_hosts(config_dir, "- {id: myforge, kind: forgejo, url: https://git.example.org}\n")
    secret = "SECRET-VALUE-12345"
    for v in ("GITHUB_TOKEN", "CODEBERG_TOKEN", "REPOHUB_MYFORGE_TOKEN"):
        monkeypatch.setenv(v, secret)
    hub = make_hub(gh(), FakeProvider("codeberg", [mk("codeberg", "o/r")]))
    for argv in (["hosts"], ["hosts", "--json"], ["search", "x", "--json"], ["repo", "codeberg:o/r"],
                 ["repo", "nowhere:o/r"], ["shelves"]):
        code, out, err = run(argv, hub)
        assert secret not in out and secret not in err


def test_shelf_json_for_unconfigured_host_is_not_live(monkeypatch):
    shelf = Shelf(name="Gone", repos=(ShelfEntry("gone", "o/r", "gnote", Snapshot("gsnap", 5)),), as_of="2026-09-01")
    monkeypatch.setattr(cli, "load_all_shelves", lambda: LoadedShelves([shelf], []))
    g = gh()
    code, out, err = run(["shelf", "0", "--json"], make_hub(g))
    doc = json.loads(out)
    assert code == 1, (out, err)  # every entry failed to refresh (partial failure would be 3)
    assert doc["repos"][0]["live"] is False and doc["repos"][0]["stars"] == 5
    assert doc["errors"] == {"gone": "host not configured"} and g.calls == 0


def test_shelves_json_lists_codeberg_shelf(monkeypatch, tmp_path):
    monkeypatch.setattr("repohub.core.browse.personal_shelves_path", lambda: tmp_path / "none.yaml")
    code, out, _ = run(["shelves", "--json"])
    doc = json.loads(out)
    cb = [s for s in doc["shelves"] if s["name"] == "Catalog: Codeberg"]
    assert code == 0 and cb and cb[0]["kind"] == "curated" and cb[0]["entries"] == 18
