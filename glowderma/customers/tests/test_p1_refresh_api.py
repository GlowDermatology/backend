# ruff: noqa: S105, S106 - test credentials and token-type literals
"""``auth/refresh/`` (customer contract §2.1.6; plan §2 P1 "Refresh")."""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

import pytest
from django.core.cache import cache
from django.db import connection
from django.test import override_settings
from django.utils import timezone

from glowderma.accounts.models import AuthSession, Customer
from glowderma.accounts.tests.factories import start_test_session
from glowderma.core import timeutils
from glowderma.core.models import AuditEntry
from glowderma.customers.tests.p1_support import FID, OTHER_FID, app_client, bearer

pytestmark = pytest.mark.django_db
REFRESH = "/api/v1/auth/refresh/"
ME = "/api/v1/me/"


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[datetime]]:
    """Starts an hour in the past (access tokens may not be issued in the future) and moves forward."""
    state = [timezone.now() - timedelta(hours=1)]
    monkeypatch.setattr(timeutils, "now", lambda: state[0])
    yield state


@pytest.fixture
def started(db: None, clock: list[datetime]) -> Any:
    customer = Customer.objects.create(phone="+998901234500", name="Rot")
    return start_test_session(customer, fid=FID)


def expire_grace(raw: str) -> None:
    """LocMem expires by the real clock; the tests move ``timeutils.now`` instead, so the 120 s grace entry
    of a rotation is dropped by hand when the moved clock is past it."""
    cache.delete(f"auth:refresh_grace:{hashlib.sha256(raw.encode()).hexdigest()}")


def refresh(token: str, fid: str = FID, platform: str = "ios") -> Any:
    return app_client(fid=fid, platform=platform).post(REFRESH, {"refresh": token})


def test_rotation_increments_the_counter_from_one_instant(started: Any, clock: list[datetime]) -> None:
    clock[0] += timedelta(minutes=1)
    response = refresh(started.raw_refresh)
    assert response.status_code == 200, response.data
    assert set(response.data) == {"access", "access_expires_at", "refresh", "refresh_expires_at"}
    session = AuthSession.objects.get(pk=started.session.pk)
    assert session.rotation == 1 and session.rotation_confirmed is False
    assert session.rotated_at == session.last_used_at == clock[0]
    assert session.absolute_expires_at == clock[0] + timedelta(days=60)  # sliding
    assert response.data["refresh_expires_at"] == timeutils.isoformat(session.absolute_expires_at)
    assert response.data["refresh"] != started.raw_refresh


def test_lost_response_replays_then_re_rotates_then_counts_as_reuse(
    started: Any, clock: list[datetime]
) -> None:
    first = refresh(started.raw_refresh).data
    clock[0] += timedelta(seconds=45)
    assert refresh(started.raw_refresh).data == first  # grace replay: identical tokens
    clock[0] += timedelta(minutes=5)
    expire_grace(started.raw_refresh)
    rerotated = refresh(started.raw_refresh)
    assert rerotated.status_code == 200
    assert rerotated.data["refresh"] != first["refresh"]
    assert refresh(started.raw_refresh).data == rerotated.data  # the grace cache was rewritten
    session = AuthSession.objects.get(pk=started.session.pk)
    assert session.revoked_at is None and session.rotation == 2
    # the token the re-rotation replaced (issued but never used) is dead: presenting it is reuse
    dead = refresh(first["refresh"])
    assert (dead.status_code, dead.data["code"]) == (401, "SESSION_EXPIRED")
    session.refresh_from_db()
    assert session.revoke_reason == "refresh_reused"
    assert AuditEntry.objects.get(action="customer.refresh_reused").after == {"sid": str(session.pk)}


def test_unconfirmed_after_the_window_is_reuse(started: Any, clock: list[datetime]) -> None:
    refresh(started.raw_refresh)
    clock[0] += timedelta(minutes=11)
    expire_grace(started.raw_refresh)
    response = refresh(started.raw_refresh)
    assert (response.status_code, response.data["code"]) == (401, "SESSION_EXPIRED")
    assert AuthSession.objects.get(pk=started.session.pk).revoke_reason == "refresh_reused"


def test_parallel_401s_get_the_grace_body_after_confirmation(started: Any, clock: list[datetime]) -> None:
    clock[0] = timezone.now()  # the access token below is checked against the real clock
    rotated = refresh(started.raw_refresh).data
    assert bearer(app_client(), rotated["access"]).get(ME).status_code == 200  # confirms rotation 1
    assert AuthSession.objects.get(pk=started.session.pk).rotation_confirmed is True
    clock[0] += timedelta(seconds=30)
    again = refresh(started.raw_refresh)
    assert again.status_code == 200 and again.data == rotated
    assert not AuditEntry.objects.filter(action="customer.refresh_reused").exists()
    assert AuthSession.objects.get(pk=started.session.pk).revoked_at is None
    # after the grace, a confirmed rotation's old token is reuse and kills the session for the new one too
    clock[0] += timedelta(seconds=120)
    expire_grace(started.raw_refresh)
    reuse = refresh(started.raw_refresh)
    assert (reuse.status_code, reuse.data["code"]) == (401, "SESSION_EXPIRED")
    assert refresh(rotated["refresh"]).data["code"] == "SESSION_EXPIRED"


def test_two_rotations_old_is_unknown(started: Any, clock: list[datetime]) -> None:
    second = refresh(started.raw_refresh).data
    clock[0] += timedelta(seconds=1)
    refresh(second["refresh"])
    response = refresh(started.raw_refresh)
    assert (response.status_code, response.data["code"]) == (401, "not_authenticated")


def test_device_mismatch_and_revoked_sessions(started: Any) -> None:
    assert refresh(started.raw_refresh, fid=OTHER_FID).data["code"] == "not_authenticated"
    assert refresh(started.raw_refresh, platform="android").data["code"] == "not_authenticated"
    AuthSession.objects.filter(pk=started.session.pk).update(
        revoked_at=timezone.now(), revoke_reason="replaced"
    )
    assert refresh(started.raw_refresh).data["code"] == "SESSION_REPLACED"
    AuthSession.objects.filter(pk=started.session.pk).update(revoke_reason="password_changed")
    assert refresh(started.raw_refresh).data["code"] == "password_changed"


@override_settings(THROTTLE_RATES={"refresh_unknown": "2/min"})
def test_junk_tokens_hit_refresh_unknown(started: Any) -> None:
    for index in range(2):
        assert refresh(f"junk-{index}").status_code == 401
    throttled = refresh("junk-3")
    assert (throttled.status_code, throttled.data["code"]) == (429, "throttled")
    assert "Retry-After" in throttled


@pytest.mark.django_db(transaction=True)
def test_concurrent_refreshes_share_one_chain() -> None:
    customer = Customer.objects.create(phone="+998901234501", name="Race")
    started = start_test_session(customer, fid=FID)
    results: list[Any] = []
    gate = threading.Event()

    def worker() -> None:
        try:
            gate.wait(10)
            results.append(refresh(started.raw_refresh).data)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for thread in threads:
        thread.start()
    gate.set()
    for thread in threads:
        thread.join(10)
    assert len(results) == 3 and all(result == results[0] for result in results)
    assert AuthSession.objects.get(pk=started.session.pk).rotation == 1
