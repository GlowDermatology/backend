from collections.abc import Callable, Iterator
from datetime import datetime

import pytest
from rest_framework.test import APIClient

from glowderma.accounts.models import StaffUser
from glowderma.accounts.tests.conftest import bearer_client
from glowderma.accounts.tests.factories import StaffUserFactory
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


@pytest.fixture
def superuser(db: None) -> StaffUser:
    """A superuser (``StaffUser.is_superuser``): the role that tests, approves, cancels and stops
    campaigns."""
    return StaffUserFactory.create(email="owner@glowderma.uz", full_name="Egasi", is_superuser=True)


@pytest.fixture
def superuser_client(superuser: StaffUser) -> APIClient:
    return bearer_client(superuser)
