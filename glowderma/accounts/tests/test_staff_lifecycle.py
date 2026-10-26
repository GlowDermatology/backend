from typing import Any, cast

import pytest
from django.core.cache import cache
from django.test import RequestFactory
from rest_framework.test import APIClient

from glowderma.accounts import services
from glowderma.accounts.models import AuthSession, StaffUser
from glowderma.accounts.tests.factories import AuthSessionFactory, StaffUserFactory, raw_refresh
from glowderma.core.api.exceptions import AuthFailed
from glowderma.core.models import AuditEntry

pytestmark = pytest.mark.django_db


@pytest.fixture
def actor() -> StaffUser:
    return StaffUserFactory.create(email="actor@x.uz", full_name="Actor")


def test_deactivate_revokes_every_session_and_audits(staff: StaffUser, actor: StaffUser) -> None:
    """Contract §1.2: SESSION_EXPIRED when the session is revoked (`deactivated`) or the staff is inactive."""
    first = AuthSessionFactory.create(staff=staff, device_label="one")
    second = AuthSessionFactory.create(staff=staff, device_label="two")
    request = RequestFactory().post("/x/", REMOTE_ADDR="203.0.113.9")
    assert services.deactivate_staff(staff, actor=actor, request=request) == 2
    assert staff.is_active is False
    staff.refresh_from_db()
    assert staff.is_active is False
    for session in (first, second):
        session.refresh_from_db()
        assert session.revoked_at is not None
        assert session.revoke_reason == "deactivated"
    entry = AuditEntry.objects.get(action="staff.deactivated")
    assert (entry.actor_id, entry.actor_label, entry.entity_type, entry.entity_id) == (
        actor.pk,
        "Actor",
        "staff",
        str(staff.pk),
    )
    assert (entry.before, entry.after) == ({"is_active": True}, {"is_active": False, "revoked_count": 2})
    assert entry.ip == "203.0.113.9"
    revoked = [
        cast(dict[str, Any], item.after)
        for item in AuditEntry.objects.filter(action="auth.session_revoked").order_by("id")
    ]
    assert sorted(item["sid"] for item in revoked) == sorted(str(s.pk) for s in (first, second))
    assert {item["reason"] for item in revoked} == {"deactivated"}
    revoked_entries = AuditEntry.objects.filter(action="auth.session_revoked")
    assert {item.entity_id for item in revoked_entries} == {str(first.pk), str(second.pk)}
    with pytest.raises(AuthFailed) as caught:
        services.refresh_staff_session(raw_refresh(first))
    assert caught.value.get_codes() == "SESSION_EXPIRED"


def test_deactivate_is_idempotent(staff: StaffUser, actor: StaffUser) -> None:
    # Active user without sessions: the state changes, nothing is revoked.
    assert services.deactivate_staff(staff, actor=actor) == 0
    assert AuditEntry.objects.get(action="staff.deactivated").after == {
        "is_active": False,
        "revoked_count": 0,
    }
    # Already inactive: revokes nothing (even a stray open session), returns 0, writes nothing. The service
    # refuses to open a session for an inactive account, so the stray one is opened while it is active.
    StaffUser.objects.filter(pk=staff.pk).update(is_active=True)
    staff.refresh_from_db()
    AuthSessionFactory.create(staff=staff)
    StaffUser.objects.filter(pk=staff.pk).update(is_active=False)
    assert services.deactivate_staff(staff, actor=actor) == 0
    assert AuthSession.objects.filter(revoked_at__isnull=True).count() == 1
    assert AuditEntry.objects.filter(action="staff.deactivated").count() == 1
    assert AuditEntry.objects.filter(action="auth.session_revoked").count() == 0
    staff.refresh_from_db()
    assert staff.is_active is False


def test_deactivate_writes_tombstones_on_commit(
    staff: StaffUser, actor: StaffUser, django_capture_on_commit_callbacks: Any
) -> None:
    session = AuthSessionFactory.create(staff=staff)
    key = f"auth:sid:{session.pk}"
    cache.set(key, f"active:{session.absolute_expires_at.timestamp()}")
    with django_capture_on_commit_callbacks(execute=True):
        assert services.deactivate_staff(staff, actor=actor) == 1
        assert str(cache.get(key)).startswith("active:")
    assert cache.get(key) == "revoked:deactivated"


def test_access_token_of_deactivated_staff_fails_with_session_expired(
    staff: StaffUser, actor: StaffUser, staff_client: APIClient, django_capture_on_commit_callbacks: Any
) -> None:
    """Contract §1.2: `SESSION_EXPIRED` — session revoked (`deactivated`) or staff inactive."""
    assert staff_client.get("/api/v1/admin/auth/me/").status_code == 200
    with django_capture_on_commit_callbacks(execute=True):
        services.deactivate_staff(staff, actor=actor)
    response = staff_client.get("/api/v1/admin/auth/me/")
    assert response.status_code == 401
    assert response.data["code"] == "SESSION_EXPIRED"


def test_reactivate_restores_login_and_audits(staff: StaffUser, actor: StaffUser) -> None:
    services.deactivate_staff(staff, actor=actor)
    assert services.reactivate_staff(staff, actor=actor) is True
    assert staff.is_active is True
    staff.refresh_from_db()
    assert staff.is_active is True
    entry = AuditEntry.objects.get(action="staff.reactivated")
    assert (entry.actor_id, entry.entity_type, entry.entity_id) == (actor.pk, "staff", str(staff.pk))
    assert entry.after == {"is_active": True}
    # Old sessions stay revoked; a fresh login works again.
    assert AuthSession.objects.filter(staff=staff, revoked_at__isnull=True).count() == 0
    session = AuthSessionFactory.create(staff=staff)
    assert services.refresh_staff_session(raw_refresh(session)).pk == session.pk


def test_reactivate_is_idempotent(staff: StaffUser, actor: StaffUser) -> None:
    assert services.reactivate_staff(staff, actor=actor) is False
    assert AuditEntry.objects.filter(action="staff.reactivated").count() == 0
