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
