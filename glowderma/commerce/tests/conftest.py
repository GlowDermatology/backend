from collections.abc import Iterator

import pytest

from glowderma.core import timeutils
from glowderma.integrations.bts import fake as fake_bts
from glowderma.integrations.click import fake as fake_click


@pytest.fixture(autouse=True)
def reset_fake_providers() -> Iterator[None]:
    """The fake BTS / Click keep their "remote" state per process: start every test from a clean slate."""
    fake_bts.reset()
    fake_click.reset()
    yield
    fake_bts.reset()
    fake_click.reset()


_REAL_NOW = timeutils.now


@pytest.fixture(autouse=True)
def clock_restored() -> Iterator[None]:
    """``pinned_clock`` swaps the process-global ``timeutils.now``: a test that leaks a pinned clock (two
    threads entering it concurrently restore each other's lambda) would freeze every later test's clock."""
    assert timeutils.now is _REAL_NOW, "a previous test leaked a pinned clock"
    yield
    leaked = timeutils.now is not _REAL_NOW
    timeutils.now = _REAL_NOW
    assert not leaked, "this test leaked a pinned clock (timeutils.now was not restored)"
