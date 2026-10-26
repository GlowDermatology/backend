"""Operator staff commands (admin contract §2.16, decision GST-6): ``deactivatestaff``, ``reactivatestaff``,
``resetstaffpassword`` and ``setstaffmanager`` go through the audited services with an operator actor."""

import re
from io import StringIO
from typing import Any
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from glowderma.accounts import services
from glowderma.accounts.models import AuthSession, StaffUser
from glowderma.accounts.tests.factories import TEST_PASSWORD, AuthSessionFactory, StaffUserFactory
from glowderma.core.models import AuditEntry

pytestmark = pytest.mark.django_db


def run(*args: Any, **options: Any) -> str:
    out = StringIO()
    call_command(*args, stdout=out, **options)
    return out.getvalue()


def test_deactivatestaff_revokes_and_audits_with_the_system_actor(manager: StaffUser) -> None:
    target = StaffUserFactory.create(email="leaver@x.uz")
    AuthSessionFactory.create(staff=target)
    assert "1 session(s) revoked" in run("deactivatestaff", "LEAVER@x.uz")
    target.refresh_from_db()
    assert target.is_active is False
    assert not AuthSession.objects.filter(staff=target, revoked_at__isnull=True).exists()
    entry = AuditEntry.objects.get(action="staff.deactivated")
    assert (entry.actor_type, entry.actor_id, entry.reason) == ("system", None, "manage.py deactivatestaff")
    revoked = AuditEntry.objects.get(action="auth.session_revoked")
    assert revoked.reason == "manage.py deactivatestaff"
    assert "already inactive" in run("deactivatestaff", "leaver@x.uz")
    assert AuditEntry.objects.filter(action="staff.deactivated").count() == 1


def test_deactivatestaff_names_the_actor(manager: StaffUser) -> None:
    target = StaffUserFactory.create(email="leaver@x.uz")
    run("deactivatestaff", "leaver@x.uz", actor="Manager@GlowDerma.uz")
    entry = AuditEntry.objects.get(action="staff.deactivated")
    assert (entry.actor_type, entry.actor_id, entry.entity_id) == ("staff", manager.pk, str(target.pk))


def test_deactivatestaff_keeps_the_last_active_manager(manager: StaffUser) -> None:
    with pytest.raises(CommandError, match=services.STAFF_LAST_MANAGER):
        run("deactivatestaff", manager.email)
    manager.refresh_from_db()
    assert manager.is_active


@pytest.mark.parametrize(
    "command,args",
    [
        ("deactivatestaff", ["nobody@x.uz"]),
        ("reactivatestaff", ["nobody@x.uz"]),
        ("resetstaffpassword", ["nobody@x.uz"]),
        ("setstaffmanager", ["nobody@x.uz"]),
    ],
)
def test_unknown_staff_is_refused(command: str, args: list[str]) -> None:
    with pytest.raises(CommandError, match="No staff account"):
        run(command, *args)


def test_an_unknown_or_inactive_actor_is_refused(manager: StaffUser) -> None:
    target = StaffUserFactory.create(email="t@x.uz")
    StaffUserFactory.create(email="gone@x.uz", is_active=False)
    for actor in ("nobody@x.uz", "gone@x.uz"):
        with pytest.raises(CommandError, match="--actor"):
            run("deactivatestaff", "t@x.uz", actor=actor)
    target.refresh_from_db()
    assert target.is_active


def test_reactivatestaff(manager: StaffUser) -> None:
    target = StaffUserFactory.create(email="back@x.uz", is_active=False)
    assert "reactivated" in run("reactivatestaff", "back@x.uz", actor=manager.email)
    target.refresh_from_db()
    assert target.is_active
    entry = AuditEntry.objects.get(action="staff.reactivated")
    assert (entry.actor_id, entry.reason) == (manager.pk, "manage.py reactivatestaff")
    assert "already active" in run("reactivatestaff", "back@x.uz")


def test_resetstaffpassword_prints_a_temporary_password_once(manager: StaffUser) -> None:
    AuthSessionFactory.create(staff=manager)
    out = run("resetstaffpassword", manager.email)  # the operator may reset the only manager's password
    match = re.search(r"Temporary password \(shown once\): (\S+)", out)
    assert match is not None
    manager.refresh_from_db()
    assert manager.check_password(match.group(1)) and not manager.check_password(TEST_PASSWORD)
    assert not AuthSession.objects.filter(staff=manager, revoked_at__isnull=True).exists()
    entry = AuditEntry.objects.get(action="staff.password_reset")
    assert (entry.reason, entry.after) == (
        "manage.py resetstaffpassword",
        {"revoked_count": 1, "generated": True},
    )
    assert match.group(1) not in str(entry.after)


def test_resetstaffpassword_from_stdin(staff: StaffUser) -> None:
    with patch("sys.stdin", StringIO("Chosen!Harbor-Long-74\n")):
        out = run("resetstaffpassword", staff.email, password_stdin=True)
    assert "Temporary password" not in out
    staff.refresh_from_db()
    assert staff.check_password("Chosen!Harbor-Long-74")
    with patch("sys.stdin", StringIO("123\n")), pytest.raises(CommandError):
        run("resetstaffpassword", staff.email, password_stdin=True)
    staff.refresh_from_db()
    assert staff.check_password("Chosen!Harbor-Long-74")


def test_setstaffmanager_grants_and_removes_the_role(manager: StaffUser) -> None:
    target = StaffUserFactory.create(email="next@x.uz")
    assert "is now a manager" in run("setstaffmanager", "next@x.uz")
    assert "already" in run("setstaffmanager", "next@x.uz")
    assert "no longer a manager" in run("setstaffmanager", manager.email, off=True)
    target.refresh_from_db()
    manager.refresh_from_db()
    assert (target.is_manager, manager.is_manager) == (True, False)
    reasons = set(AuditEntry.objects.filter(action="staff.updated").values_list("reason", flat=True))
    assert reasons == {"manage.py setstaffmanager"}
    with pytest.raises(CommandError, match=services.STAFF_LAST_MANAGER):
        run("setstaffmanager", "next@x.uz", off=True)
    assert StaffUser.objects.get(email="next@x.uz").is_manager is True
