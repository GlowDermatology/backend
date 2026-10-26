# ruff: noqa: S105, S106 - test credentials
"""Fix pass 2 regressions of ``me/*``: ``DELETE me/`` removes the customer's stored Idempotency-Key
responses, the signed-in write services require ``sid`` (``NO_SESSION`` is explicit), and the address
routes answer from the row read under the lock, not from a read after commit (customer contract §2.2.3,
§2.2.7, §6.17)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest

from glowderma.accounts import services as account_services
from glowderma.accounts.models import Address
from glowderma.core.api.exceptions import AuthFailed
from glowderma.core.models import IdempotencyRecord
from glowderma.customers import services
from glowderma.customers.tests.p1_support import PASSWORD, Signed, password_customer, reset_fakes

pytestmark = pytest.mark.django_db
ME = "/api/v1/me/"


@pytest.fixture(autouse=True)
def fakes() -> Iterator[None]:
    reset_fakes()
    yield
    reset_fakes()


# --- DELETE me/ and the stored Idempotency-Key responses


def test_delete_me_removes_the_customers_stored_idempotent_responses() -> None:
    signed = Signed(password_customer("+998907771001"))
    created = signed.client.post(ME + "addresses/", {"text": "Toshkent, Yunusobod 4"}, **signed.idem())
    assert created.status_code == 201
    record = IdempotencyRecord.objects.get(audience="customer", actor_id=signed.customer.pk)
    assert isinstance(record.response_body, dict)
    assert record.response_body["text"] == "Toshkent, Yunusobod 4"
    # a staff record with the same actor id belongs to someone else and stays
    IdempotencyRecord.objects.create(
        audience="admin",
        actor_id=signed.customer.pk,
        method="POST",
        path="/api/v1/admin/x/",
        key=record.key,
        request_hash="0" * 64,
        response_status=200,
        response_body={},
    )
    response = signed.client.delete(ME, {"password": PASSWORD})
    assert response.status_code == 204
    assert not IdempotencyRecord.objects.filter(audience="customer", actor_id=signed.customer.pk).exists()
    assert IdempotencyRecord.objects.filter(audience="admin", actor_id=signed.customer.pk).exists()


# --- sid is required


def test_signed_in_write_services_require_sid() -> None:
    customer = password_customer("+998907771002")
    with pytest.raises(TypeError, match="sid"):
        services.add_address(customer.pk, text="Toshkent, Olmazor 1")  # type: ignore[call-arg]
    with pytest.raises(TypeError, match="sid"):
        services.update_profile(customer.pk, {"name": "Malika"}, fid="fid-x")  # type: ignore[call-arg]
    assert not Address.objects.filter(customer=customer).exists()


def test_a_none_sid_fails_closed_and_no_session_is_explicit() -> None:
    customer = password_customer("+998907771003")
    with pytest.raises(AuthFailed):
        services.add_address(customer.pk, sid=None, text="Toshkent, Olmazor 2")  # type: ignore[arg-type]
    address = services.add_address(customer.pk, sid=account_services.NO_SESSION, text="Toshkent, Olmazor 3")
    assert address.customer_id == customer.pk


# --- the address routes answer from the row read under the lock


@pytest.mark.parametrize("route", ["create", "update", "default"])
def test_address_responses_survive_a_removal_committed_right_after(
    route: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent ``DELETE me/addresses/{id}/`` (or ``DELETE me/``) commits between the service and the
    response: the route still answers from the row it wrote (no ``DoesNotExist`` → 500)."""
    signed = Signed(password_customer("+998907771004"))
    first = account_services.add_address(signed.customer.pk, text="Toshkent, Chilonzor 1")
    name = {"create": "add_address", "update": "update_address", "default": "set_default_address"}[route]
    original: Callable[..., Address] = getattr(services, name)

    def then_removed(*args: Any, **kwargs: Any) -> Address:
        address = original(*args, **kwargs)
        Address.objects.filter(pk=address.pk).delete()  # the concurrent removal
        return address

    monkeypatch.setattr(services, name, then_removed)
    if route == "create":
        response = signed.client.post(ME + "addresses/", {"text": "Toshkent, Sergeli 2"}, **signed.idem())
        assert response.status_code == 201, response.data
        assert response.data["text"] == "Toshkent, Sergeli 2"
    elif route == "update":
        response = signed.client.patch(ME + f"addresses/{first.pk}/", {"label": "Uy"})
        assert response.status_code == 200, response.data
        assert response.data["label"] == "Uy"
    else:
        response = signed.client.post(ME + f"addresses/{first.pk}/default/")
        assert response.status_code == 200, response.data
        assert response.data["is_default"] is True
