"""accounts.0006_customer_auth_hardening (customer contract §1.9, §2.1.6, §2.1.8, §2.2.3)."""

import pytest
from django.core.management import call_command
from django.db import connection

from glowderma.accounts.models import AuthSession, Customer, CustomerSocialIdentity, Device


def columns(table: str) -> set[str]:
    with connection.cursor() as cursor:
        return {column.name for column in connection.introspection.get_table_description(cursor, table)}


def index_definition(name: str) -> str:
    with connection.cursor() as cursor:
        cursor.execute("SELECT indexdef FROM pg_indexes WHERE indexname = %s", [name])
        row = cursor.fetchone()
    return str(row[0]) if row else ""


@pytest.mark.django_db(transaction=True)
def test_0006_round_trip_and_history_from_backfill() -> None:
    call_command("migrate", "accounts", "0005", verbosity=0)
    assert "history_from" not in columns("accounts_customer")
    assert "key_hash" not in columns("accounts_device")
    assert index_definition("authsession_prev_refresh_idx") == ""
    with connection.cursor() as cursor:
        for phone, reactivated in (("+998901110001", "2026-01-02 10:00:00+05"), ("+998901110002", None)):
            cursor.execute(
                "INSERT INTO accounts_customer (phone, name, language, status, image, address_text,"
                " registered_at, reactivated_at, password, terms_version, created_at, updated_at)"
                " VALUES (%s, '', 'uz', 'active', '', '', now(), %s, '', '', now(), now())",
                [phone, reactivated],
            )
    call_command("migrate", "accounts", verbosity=0)
    reactivated_row = Customer.objects.get(phone="+998901110001")
    assert reactivated_row.history_from == reactivated_row.reactivated_at is not None
    assert Customer.objects.get(phone="+998901110002").history_from is None
    assert {"rotation", "rotation_confirmed"} <= columns("accounts_authsession")
    assert "provider_refresh_token" in columns("accounts_customersocialidentity")
    definition = index_definition("authsession_prev_refresh_idx")
    assert "previous_refresh_hash" in definition and "WHERE" in definition
    call_command("migrate", verbosity=0)


@pytest.mark.django_db
def test_new_fields_and_choices() -> None:
    assert ("refresh_reused", "refresh_reused") in list(
        AuthSession._meta.get_field("revoke_reason").choices or []
    )
    assert ("apple", "apple") in list(CustomerSocialIdentity._meta.get_field("provider").choices or [])
    assert Device._meta.get_field("key_hash").max_length == 64
    customer = Customer(phone="+998900000000")
    assert customer.is_authenticated is True and customer.is_anonymous is False
