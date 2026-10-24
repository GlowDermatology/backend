from collections.abc import Iterator

import pytest
from django.conf import settings
from django.core.cache import cache


@pytest.fixture(autouse=True)
def clear_cache() -> Iterator[None]:
    cache.clear()
    yield
    cache.clear()


@pytest.fixture(scope="session", autouse=True)
def static_directory() -> None:
    settings.STATIC_ROOT.mkdir(parents=True, exist_ok=True)
