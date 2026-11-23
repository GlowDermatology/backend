"""``accounts.0007`` (session sign-in method, Apple token client id) round trip."""

import pytest
from django.core.management import call_command
from django.db import connection


def columns(table: str) -> set[str]:
    with connection.cursor() as cursor:
        return {column.name for column in connection.introspection.get_table_description(cursor, table)}


@pytest.mark.django_db(transaction=True)
def test_0007_round_trip() -> None:
    call_command("migrate", "accounts", "0006", verbosity=0)
    assert "auth_method" not in columns("accounts_authsession")
    assert "provider_client_id" not in columns("accounts_customersocialidentity")
    call_command("migrate", "accounts", verbosity=0)
    assert "auth_method" in columns("accounts_authsession")
    assert "provider_client_id" in columns("accounts_customersocialidentity")
    call_command("migrate", verbosity=0)
