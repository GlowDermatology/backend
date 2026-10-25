"""payments.0001 (design 03 §9 row 13, §10): dependencies, the stored schema and reversibility."""

from importlib import import_module

import pytest
from django.core.management import call_command
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder

from glowderma.payments.tests.factories import PaymentFactory, RefundFactory

PAYMENTS_TABLES = {"payments_payment", "payments_refund"}


def table_names() -> set[str]:
    with connection.cursor() as cursor:
        return set(connection.introspection.table_names(cursor))


def index_names(table: str) -> set[str]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT indexname FROM pg_indexes WHERE tablename = %s", [table])
        return {row[0] for row in cursor.fetchall()}


def test_migration_dependencies_as_designed() -> None:
    initial = import_module("glowderma.payments.migrations.0001_initial").Migration
    assert initial.initial is True
    # commerce.0001 only (never 0002/0003: a parallel package must be able to depend on the same base)
    assert initial.dependencies == [
        ("accounts", "0004_customer_device_address_smscode_social"),
        ("commerce", "0001_initial"),
        ("core", "0007_db_extensions"),
    ]
    created = {op.name for op in initial.operations if op.__class__.__name__ == "CreateModel"}
    assert created == {"Payment", "Refund"}


def test_no_further_payments_migrations() -> None:
    from glowderma.payments import migrations as package

    names = sorted(name for name in __import__("os").listdir(package.__path__[0]) if name[0].isdigit())
    assert names == ["0001_initial.py"]


@pytest.mark.django_db(transaction=True)
def test_migrate_zero_and_forward_again() -> None:
    # Another app's transactional test may have left its dependents unapplied; start from a fully migrated
    # database and leave one behind, so the order of transactional tests does not matter.
    call_command("migrate", verbosity=0)
    assert PAYMENTS_TABLES <= table_names()
    call_command("migrate", "payments", "zero", verbosity=0)
    assert not (PAYMENTS_TABLES & table_names())
    assert not MigrationRecorder.Migration.objects.filter(app="payments").exists()
    assert "commerce_order" in table_names()  # nothing of commerce is touched on the way down
    call_command("migrate", "payments", verbosity=0)
    assert PAYMENTS_TABLES <= table_names()
    assert {
        "payment_one_active_per_order",
        "payment_provider_txn_unique",
        "payment_order_idx",
        "payment_created_idx",
        "payment_status_created_idx",
        "payment_confirmed_idx",
    } <= index_names("payments_payment")
    assert {"refund_one_open_per_payment", "refund_payment_idx", "refund_attention_idx"} <= index_names(
        "payments_refund"
    )
    assert {row.name for row in MigrationRecorder.Migration.objects.filter(app="payments")} == {
        "0001_initial"
    }
    refund = RefundFactory.create(payment=PaymentFactory.create())
    assert refund.pk is not None
    call_command("migrate", verbosity=0)  # re-apply any app that depends on payments (none today)
