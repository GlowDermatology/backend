from collections.abc import Callable, Iterator
from datetime import datetime

import pytest

from glowderma.core import seeding, timeutils
from glowderma.integrations.fcm import registry
from glowderma.integrations.fcm.fake import FakeFcm

ANCHOR = datetime.fromisoformat(seeding.DEFAULT_ANCHOR)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Callable[[datetime], None]:
    """Pin ``timeutils.now()`` to the design anchor (24 Oct 2026 12:05 Tashkent); call it to move it."""
    state = {"now": ANCHOR}
    monkeypatch.setattr(timeutils, "now", lambda: state["now"])

    def move(instant: datetime) -> None:
        state["now"] = instant

    return move


@pytest.fixture
def fcm(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeFcm]:
    """A fresh fake FCM per test so ``sent`` starts empty."""
    fake = FakeFcm()
    monkeypatch.setattr(registry, "_fake", fake)
    yield fake
