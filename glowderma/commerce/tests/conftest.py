from collections.abc import Iterator

import pytest

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
