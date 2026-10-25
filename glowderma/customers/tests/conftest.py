from collections.abc import Callable
from datetime import datetime

import pytest

from glowderma.core import seeding, timeutils
from glowderma.loyalty.models import CashbackPolicy
from glowderma.loyalty.tests.support import seed_policies

ANCHOR = datetime.fromisoformat(seeding.DEFAULT_ANCHOR)  # 2026-10-24 12:05 Tashkent


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Callable[[datetime], None]:
    """Pin ``timeutils.now()`` to the design anchor; call it to move it (before the real time only: the
    staff JWT check refuses tokens issued in the future)."""
    state = {"now": ANCHOR}
    monkeypatch.setattr(timeutils, "now", lambda: state["now"])

    def move(instant: datetime) -> None:
        state["now"] = instant

    return move


@pytest.fixture
def policies(clock: Callable[[datetime], None]) -> dict[int, CashbackPolicy]:
    """The design versions v1–v3 (03 §7.7, author NULL); v3 (0–20M 1 %, 20–30M 2 %, 30–50M 3 %) is active."""
    return seed_policies()
