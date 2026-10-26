"""accounts.0004 / 0005: reversibility and the ``customer_id`` → FK conversion (design 01 §8)."""

import uuid

import pytest
from django.core.management import call_command
from django.db import connection

from glowderma.accounts.models import AuthSession, StaffUser


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
    staff = StaffUser.objects.create_user("survivor@x.uz", "Saffron!Harbor-Long72")
    sid = uuid.uuid4()
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO accounts_authsession (id, audience, staff_id, customer_id, device_label,"
            " refresh_hash,"
            " created_at, last_used_at, absolute_expires_at, revoked_at, revoke_reason)"
            " VALUES (%s, 'staff', %s, NULL, 'legacy', %s, now(), now(), now() + interval '1 day', NULL, '')",
            [sid, staff.pk, "f" * 64],
        )
    call_command("migrate", "accounts", verbosity=0)
    assert column_is_foreign_key("accounts_authsession", "customer_id")
    session = AuthSession.objects.get(pk=sid)
    assert session.staff_id == staff.pk and session.customer_id is None
    assert session.previous_refresh_hash == "" and session.rotated_at is None and session.device_id is None


@pytest.mark.django_db(transaction=True)
def test_0005_precheck_refuses_a_stray_customer_id() -> None:
    call_command("migrate", "accounts", "0004", verbosity=0)
    staff = StaffUser.objects.create_user("stray@x.uz", "Saffron!Harbor-Long72")
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO accounts_authsession (id, audience, staff_id, customer_id, device_label,"
            " refresh_hash,"
            " created_at, last_used_at, absolute_expires_at, revoked_at, revoke_reason)"
            " VALUES (%s, 'staff', %s, 42, 'legacy', %s, now(), now(), now() + interval '1 day', NULL, '')",
            [uuid.uuid4(), staff.pk, "e" * 64],
        )
    with pytest.raises(RuntimeError, match="carry a customer_id"):
        call_command("migrate", "accounts", verbosity=0)
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM accounts_authsession")
    call_command("migrate", "accounts", verbosity=0)
    assert column_is_foreign_key("accounts_authsession", "customer_id")


def customer_indexes() -> dict[str, str]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'accounts_customer'")
        return {name: definition for name, definition in cursor.fetchall()}


@pytest.mark.django_db(transaction=True)
def test_0008_swaps_the_name_index_for_the_folded_one_and_back() -> None:
    """accounts.0008 (GC-1): the folded-name trigram index is built before the lower(name) one is dropped,
    both concurrently; the reverse restores the old index."""
    from importlib import import_module

    migration = import_module("glowderma.accounts.migrations.0008_customer_name_fold_trgm").Migration
    assert migration.atomic is False
    assert [type(op).__name__ for op in migration.operations] == [
        "AddIndexConcurrently",
        "RemoveIndexConcurrently",
    ]
    indexes = customer_indexes()
    assert "translate(lower" in indexes["accounts_customer_fold_trgm"].lower()
    assert "accounts_customer_name_trgm" not in indexes
    try:
        call_command("migrate", "accounts", "0007", verbosity=0)
        indexes = customer_indexes()
        assert "(lower((name)::text) gin_trgm_ops)" in indexes["accounts_customer_name_trgm"]
        assert "accounts_customer_fold_trgm" not in indexes
    finally:
        call_command("migrate", verbosity=0)
    indexes = customer_indexes()
    assert "accounts_customer_fold_trgm" in indexes and "accounts_customer_name_trgm" not in indexes
