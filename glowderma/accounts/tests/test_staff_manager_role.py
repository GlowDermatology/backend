"""``StaffUser.is_manager`` (admin contract §2.16, decision GST-1): migration ``accounts.0008``, the first
managers of an install and ``createstaff --manager``."""

import importlib
from io import StringIO
from unittest.mock import patch

import pytest
from django.apps import apps
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection

from glowderma.accounts import seeds
from glowderma.accounts.models import StaffUser
from glowderma.accounts.tests.factories import StaffUserFactory
from glowderma.core.models import AuditEntry

migration = importlib.import_module("glowderma.accounts.migrations.0008_staffuser_is_manager")

PASSWORD = "Long!Saffron-Harbor-72"  # noqa: S105 - test-only


def managers() -> list[str]:
    return sorted(StaffUser.objects.filter(is_manager=True).values_list("email", flat=True))


def columns(table: str) -> set[str]:
    with connection.cursor() as cursor:
        return {column.name for column in connection.introspection.get_table_description(cursor, table)}


@pytest.mark.django_db(transaction=True)
def test_0008_round_trip() -> None:
    call_command("migrate", "accounts", "0007", verbosity=0)
    assert "is_manager" not in columns("accounts_staffuser")
    call_command("migrate", "accounts", verbosity=0)
    assert "is_manager" in columns("accounts_staffuser")
    call_command("migrate", verbosity=0)


@pytest.mark.django_db
def test_data_step_promotes_superusers_then_the_first_active_account() -> None:
    old = StaffUserFactory.create(email="old@x.uz", is_active=False)  # lowest id, but inactive
    first = StaffUserFactory.create(email="first@x.uz")
    StaffUserFactory.create(email="second@x.uz")
    assert old.pk < first.pk
    migration.promote_first_managers(apps, None)
    assert managers() == ["first@x.uz"]
    migration.promote_first_managers(apps, None)  # a rerun changes nothing
    assert managers() == ["first@x.uz"]


@pytest.mark.django_db
def test_data_step_makes_every_superuser_a_manager_and_nobody_else() -> None:
    StaffUserFactory.create(email="first@x.uz")
    StaffUserFactory.create(email="root@x.uz", is_superuser=True)
    StaffUserFactory.create(email="old-root@x.uz", is_superuser=True, is_active=False)
    migration.promote_first_managers(apps, None)
    assert managers() == ["old-root@x.uz", "root@x.uz"]


@pytest.mark.django_db
def test_data_step_without_staff_does_nothing() -> None:
    migration.promote_first_managers(apps, None)
    assert not StaffUser.objects.exists()


@pytest.mark.django_db
def test_superusers_are_managers() -> None:
    assert StaffUser.objects.create_superuser("root@x.uz", PASSWORD).is_manager is True
    assert StaffUser.objects.create_user("plain@x.uz", PASSWORD).is_manager is False


@pytest.mark.django_db
def test_createstaff_first_account_is_a_manager_and_audited() -> None:
    out = StringIO()
    with patch("sys.stdin", StringIO(PASSWORD + "\n")):
        call_command("createstaff", "Owner@X.uz", password_stdin=True, full_name="Owner", stdout=out)
    assert "(manager)" in out.getvalue()
    owner = StaffUser.objects.get()
    assert (owner.email, owner.is_manager, owner.is_superuser) == ("owner@x.uz", True, False)
    entry = AuditEntry.objects.get(action="staff.created")
    assert (entry.actor_type, entry.actor_id, entry.reason, entry.entity_id) == (
        "system",
        None,
        "manage.py createstaff",
        str(owner.pk),
    )
    assert entry.after == {"email": "owner@x.uz", "full_name": "Owner", "is_manager": True}
    assert PASSWORD not in str(entry.after)


@pytest.mark.django_db
def test_createstaff_manager_flag() -> None:
    StaffUserFactory.create(email="owner@x.uz", is_manager=True)
    for email, flags in (
        ("plain@x.uz", {}),
        ("boss@x.uz", {"manager": True}),
        ("root@x.uz", {"superuser": True}),
    ):
        with patch("sys.stdin", StringIO(PASSWORD + "\n")):
            call_command("createstaff", email, password_stdin=True, stdout=StringIO(), **flags)
    assert managers() == ["boss@x.uz", "owner@x.uz", "root@x.uz"]


@pytest.mark.django_db
def test_createstaff_refuses_a_taken_email() -> None:
    StaffUserFactory.create(email="taken@x.uz", is_active=False)
    with patch("sys.stdin", StringIO(PASSWORD + "\n")), pytest.raises(CommandError):
        call_command("createstaff", "TAKEN@x.uz", password_stdin=True)
    assert StaffUser.objects.count() == 1


@pytest.mark.django_db
def test_seeded_admin_is_a_manager() -> None:
    assert seeds.ensure_admin().is_manager is True
