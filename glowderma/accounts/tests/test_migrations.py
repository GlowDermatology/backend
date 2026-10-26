"""accounts.0004 / 0005: reversibility and the ``customer_id`` → FK conversion (design 01 §8)."""

import uuid

import pytest
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from glowderma.accounts.models import AuthSession


def staff_at(migration: str, email: str) -> int:
    """A staff row written through the historical model of ``accounts.<migration>``: the current model may
    carry columns a later migration adds (``is_manager``, 0008)."""
    state = MigrationExecutor(connection).loader.project_state(("accounts", migration))
    staff_user = state.apps.get_model("accounts", "StaffUser")
    return int(staff_user.objects.create(email=email, password="!").pk)  # noqa: S106 - unusable hash


def table_exists(name: str) -> bool:
    return name in connection.introspection.table_names()


def column_is_foreign_key(table: str, column: str) -> bool:
    with connection.cursor() as cursor:
        relations = connection.introspection.get_relations(cursor, table)
    return column in relations


@pytest.mark.django_db(transaction=True)
def test_accounts_migrations_reverse_to_zero_and_forward_again() -> None:
    call_command("migrate", "accounts", "zero", verbosity=0)
    assert not table_exists("accounts_customer")
    assert not table_exists("accounts_authsession")
    call_command("migrate", verbosity=0)
    for table in ("accounts_customer", "accounts_device", "accounts_address", "accounts_smscode"):
        assert table_exists(table)
    assert table_exists("accounts_customersocialidentity")
    assert column_is_foreign_key("accounts_authsession", "customer_id")


@pytest.mark.django_db(transaction=True)
def test_authsession_rows_survive_0005() -> None:
    call_command("migrate", "accounts", "0004", verbosity=0)
    assert not column_is_foreign_key("accounts_authsession", "customer_id")
    staff_id = staff_at("0004_customer_device_address_smscode_social", "survivor@x.uz")
    sid = uuid.uuid4()
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO accounts_authsession (id, audience, staff_id, customer_id, device_label,"
            " refresh_hash,"
            " created_at, last_used_at, absolute_expires_at, revoked_at, revoke_reason)"
            " VALUES (%s, 'staff', %s, NULL, 'legacy', %s, now(), now(), now() + interval '1 day', NULL, '')",
            [sid, staff_id, "f" * 64],
        )
    call_command("migrate", "accounts", verbosity=0)
    assert column_is_foreign_key("accounts_authsession", "customer_id")
    session = AuthSession.objects.get(pk=sid)
    assert session.staff_id == staff_id and session.customer_id is None
    assert session.previous_refresh_hash == "" and session.rotated_at is None and session.device_id is None


@pytest.mark.django_db(transaction=True)
def test_0005_precheck_refuses_a_stray_customer_id() -> None:
    call_command("migrate", "accounts", "0004", verbosity=0)
    staff_id = staff_at("0004_customer_device_address_smscode_social", "stray@x.uz")
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO accounts_authsession (id, audience, staff_id, customer_id, device_label,"
            " refresh_hash,"
            " created_at, last_used_at, absolute_expires_at, revoked_at, revoke_reason)"
            " VALUES (%s, 'staff', %s, 42, 'legacy', %s, now(), now(), now() + interval '1 day', NULL, '')",
            [uuid.uuid4(), staff_id, "e" * 64],
        )
    with pytest.raises(RuntimeError, match="carry a customer_id"):
        call_command("migrate", "accounts", verbosity=0)
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM accounts_authsession")
    call_command("migrate", "accounts", verbosity=0)
    assert column_is_foreign_key("accounts_authsession", "customer_id")
