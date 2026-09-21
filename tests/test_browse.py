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
    ("bitbucket:a/b", r"shelf 'X' entry #0: unknown host 'bitbucket'"),
    ("github:../x", r"entry #0: invalid slug"),
    ({"repo": "github:o/r\n"}, r"entry #0: invalid slug"),
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
    with pytest.raises(ValueError, match=r"shelf 'X' entry #2: unknown host 'bitbucket'"):
        _one(["github:a/b", "github:c/d", "bitbucket:e/f"])


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
    b"- name: x\n  repos: [bitbucket:a/b]\n", b"!!python/object/apply:os.system ['true']\n",
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
