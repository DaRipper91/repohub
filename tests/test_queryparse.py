from __future__ import annotations

import random
import string
import time

import pytest

from repohub.core.models import SearchFilters
from repohub.core.queryparse import ParsedQuery, parse_query


def test_full_query():
    p = parse_query("tui lang:rust stars:500 days:90 host:github sort:updated nofork archived")
    assert p.text == "tui"
    assert p.filters.language == "rust"
    assert p.filters.min_stars == 500
    assert p.filters.updated_within_days == 90
    assert p.filters.hosts == ("github",)
    assert p.filters.sort == "updated"
    assert p.filters.hide_forks is True
    assert p.filters.include_archived is True
    assert p.problems == ()


def test_case_insensitive_keys_and_flags():
    p = parse_query("LANG:Go STARS:>=10 NoFork")
    assert p.filters.language == "Go"  # value keeps typed case
    assert p.filters.min_stars == 10
    assert p.filters.hide_forks is True
    assert p.problems == ()


def test_stars_gt_accepted():
    assert parse_query("stars:>10").filters.min_stars == 10
    assert parse_query("stars:>=10").filters.min_stars == 10


def test_language_alias():
    assert parse_query("language:C++").filters.language == "C++"


def test_host_both_and_days_zero():
    p = parse_query("host:both days:0", SearchFilters(hosts=("github",), updated_within_days=7))
    assert p.filters.hosts == ("github", "gitlab")
    assert p.filters.updated_within_days is None


def test_topic_lowercased():
    assert parse_query("topic:CLI-Tools").filters.topic == "cli-tools"


def test_unknown_keys_stay_words():
    p = parse_query("http://x rust:lang foo")
    assert p.text == "http://x rust:lang foo"
    assert p.problems == ()
    assert p.filters == SearchFilters()


def test_bad_values_give_problems_and_rest_works():
    p = parse_query("tui stars:abc sort:bogus host:nowhere lang:$$")
    assert p.text == "tui"
    assert len(p.problems) == 4
    assert p.filters == SearchFilters()


def test_out_of_range_numbers():
    p = parse_query("stars:99999999999 days:99999")
    assert len(p.problems) == 2
    assert p.filters == SearchFilters()


def test_negative_and_non_numeric():
    p = parse_query("stars:-5 days:1.5 stars:>")
    assert len(p.problems) == 3
    assert p.filters == SearchFilters()


def test_empty_value_needs_a_value():
    p = parse_query("foo lang:")
    assert p.text == "foo"
    assert p.problems == ("lang: needs a value",)
    assert p.filters.language is None


def test_base_respected_and_overridden():
    base = SearchFilters(min_stars=100, language="go")
    p = parse_query("stars:5", base)
    assert p.filters.min_stars == 5
    assert p.filters.language == "go"
    assert parse_query("hello", base).filters == base


def test_empty_and_whitespace():
    for raw in ("", "   ", "\t\n "):
        p = parse_query(raw)
        assert p.text == ""
        assert p.filters == SearchFilters()
        assert p.problems == ()


def test_none_safe():
    p = parse_query(None)  # type: ignore[arg-type]
    assert p.text == "" and p.filters == SearchFilters()


def test_control_characters_do_not_raise():
    p = parse_query("a\x00b \x1b[31m lang:\x00 stars:\x07")
    assert isinstance(p, ParsedQuery)


def test_very_long_input_is_fast():
    raw = " ".join(["word"] * 20000)[:100_000]
    start = time.monotonic()
    p = parse_query(raw)
    assert time.monotonic() - start < 1.0
    assert len(p.text) > 90_000
    start = time.monotonic()
    parse_query("lang:" + "a" * 100_000)
    parse_query("stars:" + "9" * 100_000)
    assert time.monotonic() - start < 1.0


def test_unicode_digits_and_underscores_rejected():
    for raw in ("stars:١٢٣", "stars:1_000", "days:１２", "stars:+5", "stars:>=٥"):
        p = parse_query(raw)
        assert len(p.problems) == 1, raw
        assert p.filters == SearchFilters(), raw
        assert p.text == ""


HOSTILE = [
    "stars:١٢٣", "stars:1_000", "stars:_1", "stars:1__0", "days:٠", "lang:😀", "lang:🦀rust",
    "topic:😀", "sort:‮updated", "sort:up\x00dated", "sort:", "sort:::", "host:GitHub​",
    "::::", ":", "::", "a:b:c:d", "stars:>>5", "stars:>=>=5", "stars:>=", "lang:" + "x" * 5000,
    "topic:" + "a" * 5000, "stars:" + "9" * 5000, "days:" + "٩" * 5000, "nofork:archived",
    "NOFORK archived", "퟿ lang:�", "lang:\U0001f980" * 50,
    "stars:\t5", "sort:ſtars", "host:ǥithub", "İ:x", "ﬂag:1", "stars:5e3", "stars:0x10",
    "stars:inf", "days:nan", "lang:" + "\n".join(["a"] * 3),
]


@pytest.mark.parametrize("raw", HOSTILE)
def test_hostile_strings_never_raise(raw):
    assert isinstance(parse_query(raw), ParsedQuery)


def test_random_garbage_never_raises():
    rng = random.Random(1234)
    alphabet = string.printable + "١٢٣😀é‮\x00_>:"
    for _ in range(300):
        raw = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 60)))
        assert isinstance(parse_query(raw), ParsedQuery)
