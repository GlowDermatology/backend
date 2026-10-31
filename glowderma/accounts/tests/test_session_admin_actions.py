"""Ops console session actions: revoke one customer session (AuthSession page and row) and every session of a
customer (the customer page) through ``accounts.sessions``."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.urls import reverse

from glowderma.accounts import sessions
from glowderma.accounts.models import AuthSession, Device
from glowderma.accounts.tests.factories import (
    AuthSessionFactory,
    CustomerFactory,
    CustomerSessionFactory,
    SignedInDeviceFactory,
)
from glowderma.core import timeutils
from glowderma.core.models import AuditEntry
from glowderma.core.tests.console import ROLES, confirm, console_client, denied, messages_of, user_of

pytestmark = pytest.mark.django_db


def revoke_url(session: AuthSession) -> str:
    return reverse("admin:accounts_authsession_revoke", args=[session.pk])


def revoke_all_url(customer_id: int) -> str:
    return reverse("admin:accounts_customer_revoke_all_sessions", args=[customer_id])


# --- one session ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("role", ROLES)
def test_revoke_permission_matrix(role: str) -> None:
    session = CustomerSessionFactory.create()
    response = confirm(console_client(role), revoke_url(session))
    session.refresh_from_db()
    if role in ("anon", "customer_like"):
        assert denied(response)
        assert session.revoked_at is None
    else:
        assert response.status_code == 302
        assert session.revoked_at is not None and session.revoke_reason == "admin_revoked"


def test_revoke_get_csrf_and_method() -> None:
    session = CustomerSessionFactory.create()
    client = console_client("staff")
    page = client.get(revoke_url(session))
    assert page.status_code == 200 and "Mijoz sessiyasini tugatish" in page.content.decode()
    assert confirm(console_client("staff", enforce_csrf_checks=True), revoke_url(session)).status_code == 403
    assert client.patch(revoke_url(session)).status_code == 405
    session.refresh_from_db()
    assert session.revoked_at is None


def test_revoke_uses_the_admin_api_service_and_audits() -> None:
    device = SignedInDeviceFactory.create()
    session = AuthSession.objects.get(device=device)
    client = console_client("staff")
    response = confirm(client, revoke_url(session))
    assert messages_of(response) == [("success", "Sessiya tugatildi.")]
    device.refresh_from_db()
    assert device.is_active is False  # no other session left on it: no more pushes
    staff_id = user_of(client).pk
    domain = AuditEntry.objects.get(action="customer.session_revoked")
    assert (domain.actor_type, domain.actor_id) == ("staff", staff_id)
    assert domain.after == {"sid": str(session.pk), "current": False}
    entry = AuditEntry.objects.get(action="ops.customer_session_revoked")
    assert (entry.actor_id, entry.entity_id, entry.after) == (
        staff_id,
        str(session.customer_id),
        {"sid": str(session.pk)},
    )


def test_revoke_repeat_and_ended_sessions_are_refused() -> None:
    session = CustomerSessionFactory.create()
    client = console_client("staff")
    confirm(client, revoke_url(session))
    page = client.get(revoke_url(session)).content.decode()
    assert "Sessiya allaqachon tugagan." in page and 'name="_ops_confirm"' not in page
    again = confirm(client, revoke_url(session))
    assert messages_of(again)[-1] == ("warning", "Sessiya allaqachon tugagan.")
    assert AuditEntry.objects.filter(action="ops.customer_session_revoked").count() == 1
    expired = CustomerSessionFactory.create(absolute_expires_at=timeutils.now() - timedelta(minutes=1))
    assert messages_of(confirm(client, revoke_url(expired)))[-1][0] == "warning"


def test_staff_sessions_are_not_revoked_here() -> None:
    staff_session = AuthSessionFactory.create()
    client = console_client("staff")
    response = confirm(client, revoke_url(staff_session))
    assert messages_of(response) == [("warning", "Xodim sessiyalari bu yerda tugatilmaydi.")]
    staff_session.refresh_from_db()
    assert staff_session.revoked_at is None
    change = client.get(reverse("admin:accounts_authsession_change", args=[staff_session.pk]))
    assert revoke_url(staff_session) not in change.content.decode()


def test_buttons_show_for_open_customer_sessions_only() -> None:
    open_session = CustomerSessionFactory.create()
    ended = CustomerSessionFactory.create(revoked_at=timeutils.now(), revoke_reason="logout")
    staff_session = AuthSessionFactory.create()
    client = console_client("staff")
    listing = client.get(reverse("admin:accounts_authsession_changelist")).content.decode()
    assert revoke_url(open_session) in listing
    assert revoke_url(ended) not in listing and revoke_url(staff_session) not in listing
    change = client.get(reverse("admin:accounts_authsession_change", args=[open_session.pk])).content
    assert revoke_url(open_session) in change.decode()


# --- every session of a customer -------------------------------------------------------------------------


@pytest.mark.parametrize("role", ROLES)
def test_revoke_all_permission_matrix(role: str) -> None:
    customer = CustomerFactory.create()
    CustomerSessionFactory.create_batch(2, customer=customer)
    response = confirm(console_client(role), revoke_all_url(customer.pk))
    remaining = sessions.open_sessions(customer.pk).count()
    if role in ("anon", "customer_like"):
        assert denied(response)
        assert remaining == 2
    else:
        assert response.status_code == 302
        assert remaining == 0


def test_revoke_all_ends_every_session_and_audits() -> None:
    customer = CustomerFactory.create()
    devices = SignedInDeviceFactory.create_batch(2, customer=customer)
    other = CustomerSessionFactory.create()  # another customer's session stays
    client = console_client("staff")
    page = client.get(revoke_all_url(customer.pk)).content.decode()
    assert "Mijozning barcha sessiyalarini tugatish" in page
    response = confirm(client, revoke_all_url(customer.pk))
    assert response["Location"] == reverse("admin:accounts_customer_change", args=[customer.pk])
    assert messages_of(response) == [("success", "2 ta sessiya tugatildi.")]
    assert set(AuthSession.objects.filter(customer=customer).values_list("revoke_reason", flat=True)) == {
        "admin_revoked"
    }
    assert not Device.objects.filter(pk__in=[d.pk for d in devices], is_active=True).exists()
    other.refresh_from_db()
    assert other.revoked_at is None
    staff_id = user_of(client).pk
    domain = AuditEntry.objects.get(action="customer.sessions_revoked")
    assert (domain.actor_id, domain.after) == (staff_id, {"count": 2, "scope": "all"})
    entry = AuditEntry.objects.get(action="ops.customer_sessions_revoked")
    assert (entry.actor_id, entry.entity_id, entry.after) == (staff_id, str(customer.pk), {"count": 2})


def test_revoke_all_without_open_sessions_is_unavailable_and_safe_to_repeat() -> None:
    customer = CustomerFactory.create()
    CustomerSessionFactory.create(customer=customer)
    client = console_client("staff")
    change_url = reverse("admin:accounts_customer_change", args=[customer.pk])
    assert revoke_all_url(customer.pk) in client.get(change_url).content.decode()
    confirm(client, revoke_all_url(customer.pk))
    assert revoke_all_url(customer.pk) not in client.get(change_url).content.decode()
    again = confirm(client, revoke_all_url(customer.pk))
    assert messages_of(again)[-1] == ("warning", "Mijozning ochiq sessiyasi yo‘q.")
    assert AuditEntry.objects.filter(action="customer.sessions_revoked").count() == 1
    assert sessions.revoke_all_sessions(customer.pk, actor=None) == 0


def test_unknown_customer_is_404() -> None:
    assert console_client("staff").get(revoke_all_url(999_999)).status_code == 404
