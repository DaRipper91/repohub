import pytest

from repohub.core import hosts


@pytest.fixture(autouse=True)
def _reset_host_registry():
    hosts.reset_registry()
    yield
    hosts.reset_registry()
