"""notifications.0003: the ``account`` inbox source of the account-security notices (contract §2.12)."""

import pytest
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.db.migrations.exceptions import IrreversibleError

from glowderma.accounts.tests.factories import CustomerFactory
from glowderma.notifications.models import InboxNotification
from glowderma.notifications.tests.factories import InboxNotificationFactory


def check_definition() -> str:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'inbox_kind_source_fields'"
        )
        row = cursor.fetchone()
    return str(row[0]) if row else ""


@pytest.mark.django_db
def test_an_account_sourced_general_row_inserts() -> None:
    customer = CustomerFactory.create()
    row = InboxNotificationFactory.create(
        customer=customer,
        kind=InboxNotification.Kind.GENERAL,
        source_type=InboxNotification.SourceType.ACCOUNT,
        source_id=customer.pk,
        link={"kind": "none"},
        dedupe_key=f"account:{customer.pk}:password_reset:{'a' * 32}",
    )
    assert row.pk and row.source_type == "account"


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("kind", "source_type", "template_status"),
    [("general", "order", ""), ("promo", "account", ""), ("general", "account", "processing")],
)
def test_the_check_refuses_other_account_combinations(
    kind: str, source_type: str, template_status: str
) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        InboxNotificationFactory.create(
            kind=kind, source_type=source_type, template_status=template_status, link={"kind": "none"}
        )


@pytest.mark.django_db(transaction=True)
def test_0003_round_trip() -> None:
    call_command("migrate", "notifications", "0002", verbosity=0)
    assert "'account'" not in check_definition()
    call_command("migrate", "notifications", verbosity=0)
    assert "'account'" in check_definition()
    call_command("migrate", verbosity=0)


@pytest.mark.django_db(transaction=True)
def test_0003_reverse_refuses_while_account_rows_exist() -> None:
    """Review nit: the old CHECK cannot hold ``account`` rows, so the reverse refuses by name instead of
    failing with an IntegrityError (or dropping notices silently)."""
    customer = CustomerFactory.create()
    row = InboxNotificationFactory.create(
        customer=customer,
        kind=InboxNotification.Kind.GENERAL,
        source_type=InboxNotification.SourceType.ACCOUNT,
        source_id=customer.pk,
        link={"kind": "none"},
        dedupe_key=f"account:{customer.pk}:password_changed:{'b' * 32}",
    )
    with pytest.raises(IrreversibleError, match="source_type='account'"):
        call_command("migrate", "notifications", "0002", verbosity=0)
    assert "'account'" in check_definition() and InboxNotification.objects.filter(pk=row.pk).exists()
    InboxNotification.objects.filter(pk=row.pk).delete()
    call_command("migrate", "notifications", "0002", verbosity=0)
    call_command("migrate", verbosity=0)
