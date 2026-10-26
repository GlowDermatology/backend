"""Follow-up of the P1 review: the address routes and a reactivation keep the customer-level lock ``FOR NO KEY
UPDATE`` (customer contract §2.7.1) instead of upgrading it to a plain ``FOR UPDATE``, so a foreign-key insert
(a capture's ``Purchase``, a new ``Order``) never waits for an address write."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from glowderma.accounts import services as account_services
from glowderma.accounts.models import Customer
from glowderma.customers.tests.p1_support import Signed, password_customer

pytestmark = pytest.mark.django_db
ADDRESSES = "/api/v1/me/addresses/"


def customer_locks(run: Callable[[], Any]) -> list[str]:
    with CaptureQueriesContext(connection) as queries:
        run()
    return [
        q["sql"].rstrip()
        for q in queries.captured_queries
        if 'FROM "accounts_customer"' in q["sql"] and q["sql"].rstrip().endswith("UPDATE")
    ]


def assert_no_key_only(locks: list[str]) -> None:
    assert locks, "the customer-level lock was not taken"
    assert all(sql.endswith("FOR NO KEY UPDATE") for sql in locks), locks


def test_address_routes_never_take_a_plain_for_update_on_the_customer() -> None:
    signed = Signed(password_customer("+998905557001"))
    client = signed.client
    answers: list[Any] = []

    def call(method: str, path: str, data: Any = None, **extra: Any) -> Callable[[], None]:
        return lambda: answers.append(getattr(client, method)(path, data, **extra))

    assert_no_key_only(
        customer_locks(call("post", ADDRESSES, {"text": "Toshkent, Chilonzor 9"}, **signed.idem()))
    )
    first = answers[-1].data
    second = client.post(ADDRESSES, {"text": "Toshkent, Sergeli 3"}, **signed.idem()).data
    for run in (
        call("patch", f"{ADDRESSES}{second['id']}/", {"comment": "3-podyezd"}),
        call("post", f"{ADDRESSES}{second['id']}/default/"),
        call("delete", f"{ADDRESSES}{first['id']}/"),
    ):
        assert_no_key_only(customer_locks(run))
    assert [answer.status_code for answer in answers] == [201, 200, 200, 204]


def test_reactivation_keeps_the_customer_lock_no_key() -> None:
    customer = password_customer("+998905557002")
    account_services.delete_customer(customer.pk)
    stored = Customer.objects.get(pk=customer.pk)
    assert_no_key_only(customer_locks(lambda: account_services.reactivate_customer(stored, name="Yangi")))
    assert Customer.objects.get(pk=customer.pk).status == "active"
