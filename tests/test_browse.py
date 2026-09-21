from repohub.core.browse import Shelf, load_shelves


def test_packaged_shelves_load_with_sane_defaults():
    shelves = load_shelves()
    assert len(shelves) >= 4
    assert all(s.name and (s.query or s.topic) for s in shelves)


def test_shelf_converts_to_filters(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text("- name: T\n  topic: tui\n  min_stars: 500\n  days: 90\n  language: Rust\n")
    s = load_shelves(p)[0]
    f = s.filters()
    assert (f.topic, f.min_stars, f.updated_within_days, f.language) == ("tui", 500, 90, "Rust")


def _write(tmp_path, text):
    p = tmp_path / "s.yaml"
    p.write_text(text)
    return p


def test_empty_shelves_file_gives_empty_list(tmp_path):
    assert load_shelves(_write(tmp_path, "")) == []


def test_top_level_must_be_a_list(tmp_path):
    import pytest
    with pytest.raises(ValueError, match="must contain a list"):
        load_shelves(_write(tmp_path, "name: x\n"))


def test_shelf_missing_name_names_the_index(tmp_path):
    import pytest
    with pytest.raises(ValueError, match=r"invalid shelf #1"):
        load_shelves(_write(tmp_path, "- name: ok\n  topic: a\n- topic: b\n"))


def test_shelf_unknown_key_raises(tmp_path):
    import pytest
    with pytest.raises(ValueError, match=r"invalid shelf #0.*bogus"):
        load_shelves(_write(tmp_path, "- name: x\n  bogus: 1\n"))


# ---- Task 7: curated shelves, validation and personal shelves ----
import pytest

from repohub.core import browse
from repohub.core.browse import (MAX_DESC, MAX_NOTE, Snapshot, ShelfEntry, load_all_shelves,
                                 parse_shelves, personal_shelves_path)


def _one(entries, name="X", **extra):
    return parse_shelves([{"name": name, "repos": entries, **extra}])[0]


def test_curated_and_search_shelves_load(tmp_path):
    shelves = parse_shelves([{"name": "S", "topic": "tui"}, {"name": "C", "repos": ["github:a/b"]}])
    assert not shelves[0].curated and shelves[1].curated
    assert shelves[1].repos == (ShelfEntry("github", "a/b"),)


def test_entry_string_and_mapping_and_snapshot():
    s = _one(["github:a/b", {"repo": "gitlab:g/sub/p", "note": "hi",
                             "snapshot": {"description": "d", "stars": 5, "language": "Go",
                                          "license": "MIT", "pushed_at": "2026-01-01"}}],
             as_of="2026-09-01")
    assert s.as_of == "2026-09-01"
    assert s.repos[0].key == "github:a/b"
    e = s.repos[1]
    assert (e.host, e.slug, e.note) == ("gitlab", "g/sub/p", "hi")
    assert e.snapshot == Snapshot("d", 5, "Go", "MIT", "2026-01-01")


@pytest.mark.parametrize("entry,msg", [
    ("Bad_Host:a/b", r"shelf 'X' entry #0: unknown host 'bad_host'"),
    ("github:../x", r"entry #0: invalid slug"),
    ({"repo": "github:o/r\nx"}, r"entry #0: invalid slug"),
    ("github:", r"entry #0: invalid slug"),
    ("github:a/b/c", r"entry #0: invalid slug"),
    ("nocolon", r"entry #0: .*host:owner/name"),
    (5, r"entry #0: expected a string or mapping"),
    ({"repo": 5}, r"entry #0: 'repo' must be a string"),
    ({"note": "x"}, r"entry #0: missing 'repo'"),
    ({"repo": "github:a/b", "bogus": 1}, r"entry #0: unknown key.*bogus"),
    ({"repo": "github:a/b", "note": 3}, r"entry #0: 'note' must be a string"),
    ({"repo": "github:a/b", "snapshot": "x"}, r"entry #0: 'snapshot' must be a mapping"),
    ({"repo": "github:a/b", "snapshot": {"zzz": 1}}, r"entry #0: snapshot: unknown key.*zzz"),
    ({"repo": "github:a/b", "snapshot": {"stars": True}}, r"entry #0: snapshot: 'stars' must be an integer"),
    ({"repo": "github:a/b", "snapshot": {"stars": "5"}}, r"entry #0: snapshot: 'stars' must be an integer"),
    ({"repo": "github:a/b", "snapshot": {"stars": -1}}, r"entry #0: snapshot: 'stars'"),
    ({"repo": "github:a/b", "snapshot": {"stars": 10_000_001}}, r"entry #0: snapshot: 'stars'"),
    ({"repo": "github:a/b", "snapshot": {"language": 3}}, r"entry #0: snapshot: 'language' must be a string"),
])
def test_entry_validation_errors(entry, msg):
    with pytest.raises(ValueError, match=msg):
        _one([entry])


def test_entry_error_names_entry_number():
    with pytest.raises(ValueError, match=r"shelf 'X' entry #2: unknown host 'bad_host'"):
        _one(["github:a/b", "github:c/d", "Bad_Host:e/f"])


def test_duplicate_entries_rejected_case_insensitively():
    with pytest.raises(ValueError, match=r"entry #1: duplicate"):
        _one(["github:Foo/Bar", {"repo": "github:foo/bar"}])


def test_note_and_snapshot_are_cleaned_and_truncated():
    evil = "a\x1b[31m‮b\nc\x9d" + "z" * 1000
    e = _one([{"repo": "github:a/b", "note": evil,
               "snapshot": {"description": evil, "license": evil}}]).repos[0]
    for v, cap in ((e.note, MAX_NOTE), (e.snapshot.description, MAX_DESC)):
        assert len(v) <= cap
        assert "\x1b" not in v and "‮" not in v and "\n" not in v and "\x9d" not in v
    assert e.note.startswith("a[31mb c")


def test_too_many_entries_and_shelves():
    with pytest.raises(ValueError, match="too many"):
        _one([f"github:a/r{i}" for i in range(1001)])
    _one([f"github:a/r{i}" for i in range(1000)])
    with pytest.raises(ValueError, match="too many"):
        parse_shelves([{"name": f"s{i}", "topic": "t"} for i in range(201)])


def test_repos_must_be_a_list():
    with pytest.raises(ValueError, match=r"shelf 'X'.*'repos' must be a list"):
        _one("github:a/b")


@pytest.mark.parametrize("item,msg", [
    ({"name": 5}, r"invalid shelf #0.*name"),
    ({"name": "x", "min_stars": True}, r"invalid shelf #0.*min_stars"),
    ({"name": "x", "min_stars": "5"}, r"invalid shelf #0.*min_stars"),
    ({"name": "x", "days": -1}, r"invalid shelf #0.*days"),
    ({"name": "x", "topic": 3}, r"invalid shelf #0.*topic"),
    ({"name": "x", "as_of": 3}, r"invalid shelf #0.*as_of"),
    ({"name": "x", "min_stars": 10**12}, r"invalid shelf #0.*min_stars"),
])
def test_shelf_field_type_errors_are_valueerror(item, msg):
    with pytest.raises(ValueError, match=msg):
        parse_shelves([item])


def test_curated_shelf_ignores_search_fields_for_filters():
    s = _one(["github:a/b"], topic="tui")
    assert s.curated


def test_parse_shelves_top_level_not_list():
    with pytest.raises(ValueError, match="must contain a list"):
        parse_shelves({"name": "x"})
    assert parse_shelves(None) == []


def test_personal_shelves_path_honours_xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert personal_shelves_path() == tmp_path / "repohub" / "shelves.yaml"


def test_load_all_shelves_order_and_catalog_optional(tmp_path):
    p = tmp_path / "mine.yaml"
    p.write_text("- name: Mine\n  repos:\n    - github:a/b\n")
    r = load_all_shelves(personal_path=p)
    assert r.problems == []
    names = [s.name for s in r.shelves]
    assert names[-1] == "Mine" and "Terminal tools" in names


def test_load_all_shelves_includes_catalog_when_present(tmp_path, monkeypatch):
    real = browse._packaged_text

    def fake(name):
        if name == "catalog_shelves.yaml":
            return "- name: Catalog\n  repos: [github:a/b]\n"
        return real(name)
    monkeypatch.setattr(browse, "_packaged_text", fake)
    p = tmp_path / "mine.yaml"
    p.write_text("- name: Mine\n  topic: x\n")
    names = [s.name for s in load_all_shelves(personal_path=p).shelves]
    assert names.index("Terminal tools") < names.index("Catalog") < names.index("Mine")


def test_packaged_problem_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(browse, "_packaged_text", lambda name: "- name: 5\n")
    with pytest.raises(ValueError):
        load_all_shelves(personal_path=tmp_path / "none.yaml")


def test_missing_personal_file_is_silent(tmp_path):
    r = load_all_shelves(personal_path=tmp_path / "nope.yaml")
    assert r.problems == [] and r.shelves


@pytest.mark.parametrize("content", [
    b"- name: [unclosed\n", b"name: x\n", b"- name: x\n  bogus: 1\n", b"\xff\xfe\x00bad",
    b"- name: x\n  repos: [Bad_Host:a/b]\n", b"!!python/object/apply:os.system ['true']\n",
    b"a: &a [1,2]\nb: *a\n" + b"#" * 2_000_000,
])
def test_broken_personal_file_gives_problem_and_defaults_load(tmp_path, content):
    p = tmp_path / "mine.yaml"
    p.write_bytes(content)
    r = load_all_shelves(personal_path=p)
    assert len(r.problems) == 1 and r.problems[0].startswith(f"personal shelves ({p}): ")
    assert [s.name for s in r.shelves][0] == "Terminal tools"
    assert "Mine" not in [s.name for s in r.shelves]


def test_personal_path_is_a_directory_is_a_problem(tmp_path):
    r = load_all_shelves(personal_path=tmp_path)
    assert len(r.problems) == 1 and r.shelves


def test_oversized_personal_file_message(tmp_path):
    p = tmp_path / "mine.yaml"
    p.write_text("- name: x\n" + "# pad\n" * 300_000)
    r = load_all_shelves(personal_path=p)
    assert "too large" in r.problems[0]


def test_deeply_nested_yaml_is_a_problem_not_a_crash(tmp_path):
    p = tmp_path / "mine.yaml"
    p.write_text("[" * 100_000)
    r = load_all_shelves(personal_path=p)
    assert len(r.problems) == 1


# ---- Task 7 review fixes ----
import datetime as _dt
import os
import threading


def test_unquoted_dates_equal_quoted_forms():
    a = parse_shelves(yaml_load("- name: A\n  as_of: 2026-09-20\n  repos:\n"
                                "    - repo: github:a/b\n      snapshot: {pushed_at: 2026-09-20}\n"))[0]
    b = parse_shelves(yaml_load("- name: A\n  as_of: '2026-09-20'\n  repos:\n"
                                "    - repo: github:a/b\n      snapshot: {pushed_at: '2026-09-20'}\n"))[0]
    assert a == b and a.as_of == "2026-09-20"
    assert a.repos[0].snapshot.pushed_at == "2026-09-20"


def yaml_load(text):
    import yaml
    return yaml.safe_load(text)


def test_datetime_keeps_full_iso_string():
    s = parse_shelves([{"name": "A", "as_of": _dt.datetime(2026, 9, 20, 1, 2, 3)}])[0]
    assert s.as_of == "2026-09-20T01:02:03"


def test_unquoted_date_in_personal_file_loads(tmp_path):
    p = tmp_path / "m.yaml"
    p.write_text("- name: Mine\n  as_of: 2026-09-20\n  repos: [github:a/b]\n")
    r = load_all_shelves(personal_path=p)
    assert r.problems == [] and r.shelves[-1].as_of == "2026-09-20"


def test_number_for_as_of_gives_quote_hint():
    with pytest.raises(ValueError, match=r"as_of must be a string \(quote it, e\.g\. '2026-09-20'\)"):
        parse_shelves([{"name": "A", "as_of": 20260920}])


def test_date_for_other_string_field_is_error_with_hint():
    with pytest.raises(ValueError, match=r"topic must be a string \(quote it"):
        parse_shelves([{"name": "A", "topic": _dt.date(2026, 1, 1)}])
    with pytest.raises(ValueError, match=r"snapshot: 'description' must be a string \(quote it"):
        _one([{"repo": "github:a/b", "snapshot": {"description": _dt.date(2026, 1, 1)}}])


def test_non_regular_personal_paths_are_problems(tmp_path):
    fifo = tmp_path / "fifo.yaml"
    os.mkfifo(fifo)
    out = {}

    def run():
        out["r"] = load_all_shelves(personal_path=fifo)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout=5)
    assert not t.is_alive(), "loader hung on a FIFO"
    assert out["r"].problems == [f"personal shelves ({fifo}): not a regular file"]
    d = load_all_shelves(personal_path=tmp_path)
    assert d.problems == [f"personal shelves ({tmp_path}): not a regular file"]


def test_symlink_to_regular_file_is_fine(tmp_path):
    real = tmp_path / "real.yaml"
    real.write_text("- name: Mine\n  topic: x\n")
    link = tmp_path / "link.yaml"
    link.symlink_to(real)
    r = load_all_shelves(personal_path=link)
    assert r.problems == [] and r.shelves[-1].name == "Mine"


def test_explicit_load_shelves_rejects_fifo(tmp_path):
    fifo = tmp_path / "f.yaml"
    os.mkfifo(fifo)
    with pytest.raises(ValueError, match="not a regular file"):
        load_shelves(fifo)


@pytest.mark.parametrize("bad", [0, False, 5, ["x"]])
def test_non_string_note_is_error(bad):
    with pytest.raises(ValueError, match=r"entry #0: 'note' must be a string"):
        _one([{"repo": "github:a/b", "note": bad}])


def test_none_note_is_empty():
    assert _one([{"repo": "github:a/b", "note": None}]).repos[0].note == ""


def test_curated_shelf_keeps_search_fields_but_is_curated():
    # Decision: search fields are accepted and kept as given; callers branch on `curated`.
    s = _one(["github:a/b"], topic="tui", min_stars=7)
    assert s.curated
    assert (s.topic, s.min_stars) == ("tui", 7)
    assert s.repos and s.filters().topic == "tui"


def test_entry_whitespace_is_stripped():
    s = _one([" github:o/r", "github:o/r2 ", {"repo": "  github:o/r3\t"}])
    assert [e.slug for e in s.repos] == ["o/r", "o/r2", "o/r3"]


def test_slug_length_cap():
    ok = "g/" + "a" * 198
    assert _one([f"gitlab:{ok}"]).repos[0].slug == ok
    with pytest.raises(ValueError, match=r"entry #0: slug too long"):
        _one(["gitlab:g/" + "a" * 199])


def test_problem_path_is_cleaned_and_capped(tmp_path):
    p = tmp_path / "bad\x1b[31mname.yaml"
    p.write_text("name: x\n")
    msg = load_all_shelves(personal_path=p).problems[0]
    assert "\x1b" not in msg
    assert len(browse._problem_text(Path("/" + "d" * 500), "boom")) < 200 + 60 + 40


from pathlib import Path  # noqa: E402


# ---- search-shelf field validation

@pytest.mark.parametrize("item,msg", [
    ({"name": "S", "language": "Rust; drop"}, r"shelf 'S'.*language"),
    ({"name": "S", "language": ""}, r"shelf 'S'.*language"),
    ({"name": "S", "topic": "Bad Topic"}, r"shelf 'S'.*topic"),
    ({"name": "S", "topic": "-x"}, r"shelf 'S'.*topic"),
    ({"name": "S", "query": "x" * 201}, r"shelf 'S'.*query.*200"),
])
def test_search_shelf_fields_are_validated(item, msg):
    with pytest.raises(ValueError, match=msg):
        parse_shelves([item])


def test_search_shelf_fields_are_normalised():
    s = parse_shelves([{"name": "S", "topic": "TUI", "language": "C++", "query": "a\nb\tc \x07d"}])[0]
    assert s.topic == "tui" and s.language == "C++" and s.query == "a b c d"
    assert parse_shelves([{"name": "S", "query": "x" * 200}])[0].query == "x" * 200


@pytest.mark.parametrize("repos", [[], None])
def test_empty_repos_is_an_error(repos):
    with pytest.raises(ValueError, match=r"shelf 'S'.*repos must not be empty"):
        parse_shelves([{"name": "S", "topic": "tui", "repos": repos}])


def test_shared_constants_come_from_models():
    from repohub.core import browse, models, queryparse
    assert models.MAX_STARS == 10_000_000 and models.MAX_DAYS == 36500
    assert not hasattr(models, "HOSTS") and not hasattr(browse, "HOSTS") and not hasattr(queryparse, "HOSTS")
    assert queryparse.LANG_RE.match("C++") and queryparse.TOPIC_RE.match("tui")


# ---- alias-bomb regression: untrusted YAML containers must never be str()'d into messages ----

def _bomb_value(kind="list", depth=9):
    if kind == "list":
        parts = ["&l0 [" + ", ".join(["x"] * 10) + "]"]
        parts += [f"&l{i} [" + ", ".join([f"*l{i-1}"] * 10) + "]" for i in range(1, depth)]
    else:
        parts = ["&l0 {" + ", ".join(f"k{j}: x" for j in range(10)) + "}"]
        parts += [f"&l{i} {{" + ", ".join(f"k{j}: *l{i-1}" for j in range(10)) + "}"
                  for i in range(1, depth)]
    return "[" + ", ".join(parts) + f", *l{depth-1}]"


def _bomb_cases():
    for kind in ("list", "dict"):
        b = _bomb_value(kind)
        yield f"- name: {b}\n"
        yield f"- name: S\n  query: {b}\n"
        yield f"- name: S\n  language: {b}\n"
        yield f"- name: S\n  topic: {b}\n"
        yield f"- name: S\n  min_stars: {b}\n"
        yield f"- name: S\n  repos: [{b}]\n"
        yield f"- name: S\n  repos: [{{repo: 'github:a/b', note: {b}}}]\n"
        yield f"- name: S\n  repos: [{{repo: {b}}}]\n"
        yield f"- name: S\n  repos: [{{repo: 'github:a/b', snapshot: {{stars: {b}}}}}]\n"
        yield f"- name: S\n  repos: [{{repo: 'github:a/b', snapshot: {{description: {b}}}}}]\n"
        yield f"- name: S\n  repos: [{{repo: 'github:a/b', snapshot: {b}}}]\n"
        yield f"- name: S\n  extra: {b}\n"
        yield f"- {b}\n"
        yield f"- name: S\n  repos: {b}\n"


def test_alias_bombs_do_not_expand_in_messages(tmp_path):
    import time

    for i, text in enumerate(_bomb_cases()):
        p = tmp_path / f"s{i}.yaml"
        p.write_text(text, encoding="utf-8")
        box = {}

        def run():
            box["r"] = load_all_shelves(p)

        t0 = time.perf_counter()
        th = threading.Thread(target=run, daemon=True)
        th.start()
        th.join(timeout=5)
        assert not th.is_alive(), text[:80]
        assert time.perf_counter() - t0 < 2, text[:80]
        problems = box["r"].problems
        assert len(problems) == 1, text[:80]
        assert len(problems[0]) < 500


def test_show_describes_containers_by_type_only():
    assert browse._show([1, 2]) == "<list>"
    assert browse._show({"a": 1}) == "<dict>"
    assert browse._show(5) == "5" and browse._show(None) == "None" and browse._show(True) == "True"
    assert browse._show("a\x1b[31m" + "x" * 100) == repr(("a[31m" + "x" * 100)[:60])


def test_entry_for_unconfigured_but_valid_host_is_kept():
    s = _one(["forgejo-x:a/b", "selfhost:g/sub/p"])
    assert [e.key for e in s.repos] == ["forgejo-x:a/b", "selfhost:g/sub/p"]


@pytest.mark.parametrize("entry", ["selfhost:../x", "selfhost:o", "selfhost:", "selfhost:a/b\nc", "1bad:a/b", "sp ace:a/b"])
def test_entry_for_unconfigured_host_still_needs_safe_slug_and_id(entry):
    with pytest.raises(ValueError):
        _one([entry])


def test_configured_host_uses_registry_slug_rule():
    assert _one(["codeberg:o/r"]).repos[0].key == "codeberg:o/r"
    with pytest.raises(ValueError, match="invalid slug"):
        _one(["codeberg:g/sub/p"])
    with pytest.raises(ValueError, match="invalid slug"):
        _one(["github:g/sub/p"])
    assert _one(["gitlab:g/sub/p"]).repos[0].slug == "g/sub/p"


def test_custom_gitlab_kind_host_allows_nested_slug():
    from repohub.core.hosts import BUILTIN_HOSTS, HostRegistry, HostSpec, set_registry
    set_registry(HostRegistry(BUILTIN_HOSTS + (HostSpec("mylab", "gitlab", "My Lab", "git.example.org", "https://git.example.org/api/v4"),)))
    assert _one(["mylab:g/sub/p"]).repos[0].slug == "g/sub/p"


def test_load_all_shelves_packaged_order_defaults_catalog_codeberg_personal(tmp_path):
    p = tmp_path / "mine.yaml"
    p.write_text("- name: Mine\n  repos:\n    - github:a/b\n")
    names = [s.name for s in load_all_shelves(personal_path=p).shelves]
    assert (names.index("Terminal tools") < names.index("Catalog: Terminal & TUI")
            < names.index("Catalog: Codeberg") < names.index("Mine"))
    assert names[-2:] == ["Catalog: Codeberg", "Mine"]


def test_missing_optional_codeberg_file_is_not_an_error(tmp_path, monkeypatch):
    real = browse._packaged_text
    monkeypatch.setattr(browse, "_packaged_text", lambda n: None if n == "catalog_codeberg.yaml" else real(n))
    r = load_all_shelves(personal_path=tmp_path / "none.yaml")
    assert r.problems == [] and "Catalog: Codeberg" not in [s.name for s in r.shelves]


def test_broken_codeberg_file_still_raises(tmp_path, monkeypatch):
    real = browse._packaged_text
    monkeypatch.setattr(browse, "_packaged_text",
                        lambda n: "- name: 5\n" if n == "catalog_codeberg.yaml" else real(n))
    with pytest.raises(ValueError):
        load_all_shelves(personal_path=tmp_path / "none.yaml")


def test_entry_for_unconfigured_host_loads_but_hostile_slug_is_rejected():
    s = browse.parse_shelves([{"name": "X", "repos": ["gone:o/r"]}])[0]
    assert s.repos[0].host == "gone"
    with pytest.raises(ValueError, match="invalid slug"):
        browse.parse_shelves([{"name": "X", "repos": ["gone:../../etc/passwd"]}])
    with pytest.raises(ValueError, match="invalid slug"):
        browse.parse_shelves([{"name": "X", "repos": ["gone:o/r?x=<script>"]}])
