import pytest

from repohub.core.textsafe import clean_text


@pytest.mark.parametrize("raw,expected", [
    ("a\x1b[2Jb", "a[2Jb"),
    ("a\x1b]0;x\x1b\\b", "a]0;x\\b"),
    ("a\x00b", "ab"),
    ("a‮b", "ab"),
    ("a⁦b⁩c‎d‏", "abcd"),
    ("a\x9bb\x7fc\x85d", "abcd"),
    ("héllo 日本語 🚀", "héllo 日本語 🚀"),
])
def test_removes_control_and_bidi(raw, expected):
    assert clean_text(raw) == expected
    assert clean_text(raw, multiline=True) == expected


def test_none_is_empty():
    assert clean_text(None) == ""


def test_single_line_collapses_whitespace():
    assert clean_text("a\n\tb\r\n  c") == "a b c"


def test_multiline_keeps_newline_and_tab_but_not_other_controls():
    assert clean_text("a\n\tb\x07\r", multiline=True) == "a\n\tb"
