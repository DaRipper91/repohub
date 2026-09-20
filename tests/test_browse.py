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
