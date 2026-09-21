import re
import time
from importlib import resources
from pathlib import Path

import pytest

from repohub.core.browse import load_all_shelves

EXPECTED_NAMES = [
    "Catalog: Terminal & TUI",
    "Catalog: Data, Maps & OSINT",
    "Catalog: Local AI",
    "Catalog: Networking",
    "Catalog: Retro, Games & Creative",
    "Catalog: Self-Hosted & Home",
    "Catalog: Developer Tools",
    "Catalog: Hardware, Radio & Phones",
]
BAD_CHARS = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f‎‏‪-‮⁦-⁩]")


@pytest.fixture
def loaded(tmp_path):
    return load_all_shelves(personal_path=tmp_path / "none.yaml")


@pytest.fixture
def catalog(loaded):
    return [s for s in loaded.shelves if s.name.startswith("Catalog: ")]


def test_packaged_catalog_follows_search_shelves(loaded, catalog):
    assert loaded.problems == []
    assert len(catalog) == 8
    n_search = len(loaded.shelves) - 8
    assert n_search >= 1
    assert all(not s.curated for s in loaded.shelves[:n_search])
    assert loaded.shelves[n_search:] == catalog


def test_shelf_order_is_stable(catalog):
    assert [s.name for s in catalog] == EXPECTED_NAMES


def test_catalog_shelves_are_curated_with_as_of(catalog):
    assert all(s.curated for s in catalog)
    assert all(s.as_of == "2026-09-20" for s in catalog)


def test_catalog_has_171_entries(catalog):
    assert sum(len(s.repos) for s in catalog) == 171


def test_every_entry_has_note_and_snapshot(catalog):
    for s in catalog:
        for e in s.repos:
            assert e.note.strip(), e.key
            assert e.snapshot is not None, e.key
            assert e.snapshot.description.strip(), e.key
            assert isinstance(e.snapshot.stars, int) and not isinstance(e.snapshot.stars, bool)
            assert e.snapshot.stars >= 0, e.key


def test_both_hosts_occur(catalog):
    hosts = {e.host for s in catalog for e in s.repos}
    assert "gitlab" in hosts and "github" in hosts


def test_no_duplicate_keys(catalog):
    keys = [e.key for s in catalog for e in s.repos]
    assert len(keys) == len(set(keys))


def test_no_control_characters(catalog):
    for s in catalog:
        assert not BAD_CHARS.search(s.name), s.name
        for e in s.repos:
            assert not BAD_CHARS.search(e.note), e.key
            assert not BAD_CHARS.search(e.snapshot.description), e.key


def test_catalog_file_is_packaged():
    assert resources.files("repohub.core").joinpath("catalog_shelves.yaml").is_file()


def test_catalog_lives_inside_package_dir():
    # hatch includes non-Python files inside the package by default: no explicit include needed
    import repohub.core
    pkg = Path(repohub.core.__file__).resolve().parent
    assert pkg.parts[-3:] == ("src", "repohub", "core")
    assert (pkg / "catalog_shelves.yaml").is_file()


def test_loading_is_fast_and_uses_explicit_personal_path(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("real config dir touched")
    monkeypatch.setattr("repohub.core.browse.personal_shelves_path", boom)
    t = time.perf_counter()
    result = load_all_shelves(personal_path=tmp_path / "none.yaml")
    assert time.perf_counter() - t < 5.0
    assert result.problems == []
