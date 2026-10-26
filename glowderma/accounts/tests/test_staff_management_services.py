"""Staff management services (admin contract §2.16; decisions GST-1..GST-4): create, update, reset the
password, the self and last-active-manager guards (also under concurrency), audit."""

import re
import threading
from collections.abc import Callable
from typing import Any

import pytest
from django.contrib.auth import authenticate
from django.db import connection
from rest_framework.exceptions import ValidationError

from glowderma.accounts import services
from glowderma.accounts.models import AuthSession, SocialIdentity, StaffUser
from glowderma.accounts.tests.factories import TEST_PASSWORD, AuthSessionFactory, StaffUserFactory
from glowderma.core.api.exceptions import Conflict
from glowderma.core.models import AuditEntry

TEMPORARY = re.compile(r"^[A-Za-z2-9]{4}(-[A-Za-z2-9]{4}){3}$")


def conflict_message(call: Callable[[], Any]) -> str:
    with pytest.raises(Conflict) as caught:
        call()
    return str(caught.value.detail)


@pytest.mark.django_db
def test_create_staff_generates_a_valid_temporary_password_and_audits(manager: StaffUser) -> None:
    user, password = services.create_staff(
        email="  Nodira@GlowDerma.uz ", full_name="Nodira Rahimova", actor=manager
    )
    assert TEMPORARY.fullmatch(password)
    assert not set(password) & set("0O1lI")
    user.refresh_from_db()
    assert (user.email, user.full_name, user.is_active, user.is_manager, user.is_superuser) == (
        "nodira@glowderma.uz",
        "Nodira Rahimova",
        True,
        False,
        False,
    )
    assert authenticate(email="nodira@glowderma.uz", password=password) == user
    entry = AuditEntry.objects.get(action="staff.created")
    assert (entry.actor_id, entry.entity_type, entry.entity_id) == (manager.pk, "staff", str(user.pk))
    assert entry.after == {
        "email": "nodira@glowderma.uz",
        "full_name": "Nodira Rahimova",
        "is_manager": False,
    }
    assert password not in str(entry.after)


@pytest.mark.django_db
def test_create_staff_refuses_an_email_in_use_by_any_account(manager: StaffUser) -> None:
    StaffUserFactory.create(email="old@glowderma.uz", is_active=False)
    with pytest.raises(ValidationError) as caught:
        services.create_staff(email="OLD@glowderma.uz", full_name="Yangi", actor=manager)
    detail: Any = caught.value.detail
    assert [(str(item), item.code) for item in detail["email"]] == [(services.STAFF_EMAIL_TAKEN, "unique")]
    assert not AuditEntry.objects.filter(action="staff.created").exists()


@pytest.mark.django_db
def test_the_first_account_without_an_active_manager_becomes_one() -> None:
    StaffUserFactory.create(email="old-boss@x.uz", is_manager=True, is_active=False)
    first, _ = services.create_staff(email="first@x.uz", full_name="Birinchi", actor=None)
    second, _ = services.create_staff(email="second@x.uz", full_name="Ikkinchi", actor=None)
    assert (first.is_manager, second.is_manager) == (True, False)


@pytest.mark.django_db
def test_update_writes_and_audits_only_what_changed(manager: StaffUser) -> None:
    target = StaffUserFactory.create(full_name="Eski ism")
    services.update_staff(target, {"full_name": "Eski ism", "is_manager": False}, actor=manager)
    assert not AuditEntry.objects.filter(action="staff.updated").exists()
    services.update_staff(target, {"full_name": "Yangi ism", "is_manager": True}, actor=manager)
    target.refresh_from_db()
    assert (target.full_name, target.is_manager) == ("Yangi ism", True)
    entry = AuditEntry.objects.get(action="staff.updated")
    assert (entry.before, entry.after) == (
        {"full_name": "Eski ism", "is_manager": False},
        {"full_name": "Yangi ism", "is_manager": True},
    )
    with pytest.raises(ValueError, match="email"):
        services.update_staff(target, {"email": "x@x.uz"}, actor=manager)


@pytest.mark.django_db
def test_self_guards(manager: StaffUser) -> None:
    StaffUserFactory.create(is_manager=True)  # another active manager: only the self rule refuses
    assert conflict_message(lambda: services.deactivate_staff(manager, actor=manager)) == (
        services.STAFF_SELF_DEACTIVATE
    )
    assert conflict_message(lambda: services.update_staff(manager, {"is_manager": False}, actor=manager)) == (
        services.STAFF_SELF_DEMOTE
    )
    assert conflict_message(lambda: services.reset_staff_password(manager, actor=manager)) == (
        services.STAFF_SELF_RESET
    )
    manager.refresh_from_db()
    assert manager.is_active and manager.is_manager and manager.check_password(TEST_PASSWORD)
    # The operator commands may act on the actor's own account.
    services.update_staff(manager, {"is_manager": False}, actor=manager, refuse_self=False)
    manager.refresh_from_db()
    assert manager.is_manager is False


@pytest.mark.django_db
def test_the_last_active_manager_keeps_the_role_and_the_access(manager: StaffUser) -> None:
    StaffUserFactory.create(is_manager=True, is_active=False)  # an inactive manager does not count
    assert (
        conflict_message(lambda: services.deactivate_staff(manager, actor=None))
        == services.STAFF_LAST_MANAGER
    )
    assert conflict_message(lambda: services.update_staff(manager, {"is_manager": False}, actor=None)) == (
        services.STAFF_LAST_MANAGER
    )
    successor = StaffUserFactory.create()
    services.update_staff(successor, {"is_manager": True}, actor=None)
    assert services.deactivate_staff(manager, actor=successor) == 0
    manager.refresh_from_db()
    assert manager.is_active is False and manager.is_manager is True  # the role is kept for a reactivation
    # A non-manager and an inactive manager may always lose the role.
    other = StaffUserFactory.create(is_manager=True, is_active=False)
    services.update_staff(other, {"is_manager": False}, actor=successor)


@pytest.mark.django_db
def test_deactivate_keeps_the_social_identities(manager: StaffUser, staff: StaffUser) -> None:
    SocialIdentity.objects.create(staff=staff, provider="google", subject="g-1", email_at_link=staff.email)
    services.deactivate_staff(staff, actor=manager)
    assert SocialIdentity.objects.filter(staff=staff).count() == 1


@pytest.mark.django_db
def test_reset_password_revokes_every_session_and_audits(manager: StaffUser, staff: StaffUser) -> None:
    sessions = [AuthSessionFactory.create(staff=staff) for _ in range(2)]
    SocialIdentity.objects.create(staff=staff, provider="google", subject="g-1", email_at_link=staff.email)
    password, revoked = services.reset_staff_password(staff, actor=manager)
    assert revoked == 2 and TEMPORARY.fullmatch(password)
    staff.refresh_from_db()
    assert staff.check_password(password) and not staff.check_password(TEST_PASSWORD)
    assert set(AuthSession.objects.filter(staff=staff).values_list("revoke_reason", flat=True)) == {
        "password_changed"
    }
    assert SocialIdentity.objects.filter(staff=staff).exists()
    entry = AuditEntry.objects.get(action="staff.password_reset")
    assert (entry.actor_id, entry.entity_id, entry.after) == (
        manager.pk,
        str(staff.pk),
        {"revoked_count": 2, "generated": True},
    )
    revoked_rows = AuditEntry.objects.filter(action="auth.session_revoked")
    assert {row.entity_id for row in revoked_rows} == {str(session.pk) for session in sessions}
    assert {row.after["reason"] for row in revoked_rows if isinstance(row.after, dict)} == {
        "password_changed"
    }


@pytest.mark.django_db
def test_reset_password_with_a_given_password_on_an_inactive_account(manager: StaffUser) -> None:
    target = StaffUserFactory.create(is_active=False)
    password, revoked = services.reset_staff_password(
        target,
        actor=None,
        password="Given!Harbor-Long-73",  # noqa: S106 - test-only
        reason="manage.py resetstaffpassword",
    )
    target.refresh_from_db()
    assert (password, revoked) == ("Given!Harbor-Long-73", 0) and target.check_password(password)
    entry = AuditEntry.objects.get(action="staff.password_reset")
    assert (entry.actor_type, entry.reason, entry.after) == (
        "system",
        "manage.py resetstaffpassword",
        {"revoked_count": 0, "generated": False},
    )


@pytest.mark.django_db(transaction=True)
def test_two_managers_removing_each_other_leave_one_active_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    """The active managers are locked before the check: the second write waits for the first one and then
    sees its result (the last active manager is refused), instead of both passing and leaving none."""
    first = StaffUserFactory.create(email="a@x.uz", is_manager=True)
    second = StaffUserFactory.create(email="b@x.uz", is_manager=True)
    locked = threading.Event()
    release = threading.Event()
    original = services._lock_active_managers

    def slow_lock() -> list[int]:
        ids = original()
        if threading.current_thread().name == "demote":
            locked.set()
            assert release.wait(10)
        return ids

    monkeypatch.setattr(services, "_lock_active_managers", slow_lock)
    outcomes: dict[str, str] = {}

    def run(name: str, call: Callable[[], Any]) -> None:
        try:
            call()
            outcomes[name] = "ok"
        except Conflict as exc:
            outcomes[name] = str(exc.detail)
        finally:
            connection.close()

    demote = threading.Thread(
        name="demote",
        target=run,
        args=("demote", lambda: services.update_staff(second, {"is_manager": False}, actor=first)),
    )
    deactivate = threading.Thread(
        name="deactivate",
        target=run,
        args=("deactivate", lambda: services.deactivate_staff(first, actor=second)),
    )
    demote.start()
    assert locked.wait(10)
    deactivate.start()
    deactivate.join(1)
    assert deactivate.is_alive()  # waiting for the managers' locks held by the demotion
    release.set()
    demote.join(10)
    deactivate.join(10)
    assert outcomes == {"demote": "ok", "deactivate": services.STAFF_LAST_MANAGER}
    assert list(
        StaffUser.objects.filter(is_active=True, is_manager=True).values_list("email", flat=True)
    ) == ["a@x.uz"]
