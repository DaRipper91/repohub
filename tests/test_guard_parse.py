import pytest

from repohub.core.providers.base import ProviderError, guard_parse


class Fake:
    host = "fake"

    def __init__(self, exc):
        self.exc = exc

    @guard_parse
    async def call(self):
        raise self.exc


@pytest.mark.parametrize("exc", [OverflowError("x"), RecursionError("x"), KeyError("k"), ValueError("v")])
async def test_guard_parse_maps_malformed_exceptions(exc):
    with pytest.raises(ProviderError, match="unexpected response") as ei:
        await Fake(exc).call()
    assert ei.value.host == "fake"


async def test_guard_parse_lets_provider_error_through():
    with pytest.raises(ProviderError, match="boom"):
        await Fake(ProviderError("fake", "boom")).call()
