from dataclasses import asdict

import pytest

from repohub.core.models import SORTS, Asset, Release, Repo, SearchFilters, parse_arch


@pytest.mark.parametrize("name,arch", [
    ("tool-linux-aarch64.tar.gz", "arm64"),
    ("app-arm64-v8a.apk", "arm64"),
    ("tool_1.0_armv8.deb", "arm64"),
    ("tool_1.0_amd64.deb", "x86_64"),
    ("tool-x86_64-unknown-linux-gnu.tar.gz", "x86_64"),
    ("tool-win-x64.zip", "x86_64"),
    ("tool-source.zip", "unknown"),
])
def test_parse_arch(name, arch):
    assert parse_arch(name) == arch


def make_repo(**kw):
    base = dict(host="github", slug="Octo/Cat", url="https://github.com/Octo/Cat", description="d",
                stars=1, language="Go", license="MIT", topics=("a", "b"), pushed_at="2026-09-01T00:00:00Z",
                archived=False, forks=2, homepage="")
    base.update(kw)
    return Repo(**base)


def test_repo_key_is_lowercase_and_host_scoped():
    assert make_repo().key == "github:octo/cat"


def test_repo_roundtrip_through_dict():
    r = make_repo()
    assert Repo.from_dict(r.to_dict()) == r


def test_release_roundtrip_and_arm64_flag():
    rel = Release("v1", "2026-09-01T00:00:00Z", (Asset("a-arm64.tgz", 10, "https://x/a", "arm64"),))
    assert rel.has_arm64
    assert Release.from_dict(rel.to_dict()) == rel
    assert not Release("v1", None, ()).has_arm64


def test_filters_default_to_both_hosts_and_hide_archived():
    f = SearchFilters()
    assert f.hosts == ("github", "gitlab") and f.include_archived is False


def test_old_cached_row_without_fork_loads():
    row = make_repo().to_dict()
    del row["fork"]
    assert Repo.from_dict(row).fork is False


def test_fork_round_trips():
    assert Repo.from_dict(make_repo(fork=True).to_dict()).fork is True


def test_search_filters_new_defaults():
    f = SearchFilters()
    assert f.sort == "stars"
    assert f.hide_forks is False
    assert SORTS == ("stars", "updated", "forks")


def test_search_filters_cache_key_covers_new_fields():
    assert asdict(SearchFilters(sort="updated")) != asdict(SearchFilters())
    assert asdict(SearchFilters(hide_forks=True)) != asdict(SearchFilters())
