from collections.abc import Callable
from datetime import datetime

import pytest

from glowderma.core import seeding, timeutils

ANCHOR = datetime.fromisoformat(seeding.DEFAULT_ANCHOR)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Callable[[datetime], None]:
    """Pin ``timeutils.now()`` to the design anchor (24 Oct 2026 12:05 Tashkent); call it to move it."""
    state = {"now": ANCHOR}
    monkeypatch.setattr(timeutils, "now", lambda: state["now"])

    def move(instant: datetime) -> None:
        state["now"] = instant

    return move
