import pytest

from repohub.core.providers.base import valid_slug


def test_valid_slugs():
    assert valid_slug("o/r", "github")
    assert valid_slug("g/sub/p", "gitlab")
    assert not valid_slug("g/sub/p", "github")


@pytest.mark.parametrize("bad", ["o/r\n", "o/..\n", "../x", "o", "o/r ", "o/r%2F", ""])
def test_invalid_slugs(bad):
    assert not valid_slug(bad, "github")
    assert not valid_slug(bad, "gitlab")
