# ruff: noqa: S105, S106 - test credentials
"""Follow-up of the P1 review on ``auth/refresh/`` (customer contract §2.1.6): reuse is decided before the
device check, so a stale token presented from another install revokes the session (step 6) instead of
answering a silent 401 ``not_authenticated``; the ``customer.refresh_reused`` audit carries the request."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

import pytest
from django.core.cache import cache
from django.utils import timezone

from glowderma.accounts.models import AuthSession, Customer
from glowderma.accounts.tests.factories import start_test_session
from glowderma.core import timeutils
from glowderma.core.models import AuditEntry
from glowderma.customers.tests.p1_support import FID, OTHER_FID, app_client

pytestmark = pytest.mark.django_db
REFRESH = "/api/v1/auth/refresh/"


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[datetime]]:
    state = [timezone.now() - timedelta(hours=1)]
    monkeypatch.setattr(timeutils, "now", lambda: state[0])
    yield state


@pytest.fixture
def started(db: None, clock: list[datetime]) -> Any:
    customer = Customer.objects.create(phone="+998901234590", name="Rot")
    return start_test_session(customer, fid=FID)


def expire_grace(raw: str) -> None:
    cache.delete(f"auth:refresh_grace:{hashlib.sha256(raw.encode()).hexdigest()}")


def refresh(token: str, fid: str = FID, **extra: Any) -> Any:
    return app_client(fid=fid, **extra).post(REFRESH, {"refresh": token})


def assert_reused(started: Any, response: Any) -> AuditEntry:
    assert (response.status_code, response.data["code"]) == (401, "SESSION_EXPIRED")
    assert AuthSession.objects.get(pk=started.session.pk).revoke_reason == "refresh_reused"
    entry = AuditEntry.objects.get(action="customer.refresh_reused")
    assert entry.after == {"sid": str(started.session.pk)}
    return entry


def test_a_stale_token_from_another_install_is_reuse(started: Any, clock: list[datetime]) -> None:
    refresh(started.raw_refresh)
    clock[0] += timedelta(minutes=11)  # past the grace and the re-rotation window
    expire_grace(started.raw_refresh)
    response = refresh(started.raw_refresh, fid=OTHER_FID, REMOTE_ADDR="203.0.113.9")
    entry = assert_reused(started, response)
    assert str(entry.ip) == "203.0.113.9" and entry.request_id  # the audit carries the request


def test_a_dead_token_from_another_install_is_reuse(started: Any, clock: list[datetime]) -> None:
    first = refresh(started.raw_refresh).data
    clock[0] += timedelta(minutes=5)
    expire_grace(started.raw_refresh)
    assert refresh(started.raw_refresh).status_code == 200  # re-rotation: first["refresh"] is dead now
    assert_reused(started, refresh(first["refresh"], fid=OTHER_FID))


def test_token_answers_still_require_the_sessions_device(started: Any, clock: list[datetime]) -> None:
    """Rotation, the grace replay and the re-rotation hand out tokens: another install gets 401
    ``not_authenticated`` and nothing is revoked."""
    assert refresh(started.raw_refresh, fid=OTHER_FID).data["code"] == "not_authenticated"  # rotation
    refresh(started.raw_refresh)
    clock[0] += timedelta(seconds=30)
    assert refresh(started.raw_refresh, fid=OTHER_FID).data["code"] == "not_authenticated"  # grace
    clock[0] += timedelta(minutes=3)
    expire_grace(started.raw_refresh)
    assert refresh(started.raw_refresh, fid=OTHER_FID).data["code"] == "not_authenticated"  # re-rotation
    assert AuthSession.objects.get(pk=started.session.pk).revoked_at is None
    assert not AuditEntry.objects.filter(action="customer.refresh_reused").exists()
