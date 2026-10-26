"""``dev/payments/{order_id}/simulate/`` (customer contract §2.15): mounted only under the local/test settings
with ``FAKE_PAYMENTS_ENABLED`` and fake payment modes; runs the real webhook views."""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from datetime import timedelta

import pytest
from django.test import override_settings
from django.urls import clear_url_caches
from rest_framework.test import APIClient

from glowderma.accounts.models import Customer
from glowderma.commerce.models import Order
from glowderma.commerce.tests.support import place, seed_policies, shopper
from glowderma.core import timeutils
from glowderma.payments.models import Payment

pytestmark = pytest.mark.django_db


@pytest.fixture
def dev_routes() -> Iterator[None]:
    import config.urls

    with override_settings(FAKE_PAYMENTS_ENABLED=True):
        importlib.reload(config.urls)
        clear_url_caches()
        yield
    importlib.reload(config.urls)
    clear_url_caches()


def test_absent_without_the_flag(customer_client: APIClient, customer: Customer) -> None:
    order = place(customer, fulfilment="pickup")
    response = customer_client.post(
        f"/api/v1/dev/payments/{order.pk}/simulate/", {"outcome": "success"}, format="json"
    )
    assert response.status_code == 404


def test_absent_under_the_production_posture() -> None:
    import config.urls

    with override_settings(FAKE_PAYMENTS_ENABLED=True, DEV_FEATURES_ALLOWED=False):
        assert config.urls.dev_routes_enabled() is False
    with override_settings(FAKE_PAYMENTS_ENABLED=True, CLICK_MODE="live"):
        assert config.urls.dev_routes_enabled() is False


@pytest.mark.parametrize("provider", ["click", "payme"])
def test_simulate_success(
    dev_routes: None, customer_client: APIClient, customer: Customer, provider: str
) -> None:
    seed_policies()
    order = place(customer, fulfilment="pickup", provider=provider)
    _fresh(order)
    response = customer_client.post(
        f"/api/v1/dev/payments/{order.pk}/simulate/",
        {"outcome": "success", "provider": provider},
        format="json",
    )
    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["order_id"] == order.pk and body["provider"] == provider
    assert [step["call"] for step in body["steps"]] == (
        ["prepare", "complete"] if provider == "click" else ["check_perform", "create", "perform"]
    )
    order.refresh_from_db()
    assert order.status == "processing"


def test_simulate_failure_and_ownership(
    dev_routes: None, customer_client: APIClient, customer: Customer
) -> None:
    order = place(customer, fulfilment="pickup")
    _fresh(order)
    body = customer_client.post(
        f"/api/v1/dev/payments/{order.pk}/simulate/", {"outcome": "fail"}, format="json"
    ).json()
    assert body["steps"][-1]["response"]["error"] == -9
    assert Payment.objects.get(order=order).status == "failed"
    other = place(shopper(), fulfilment="pickup")
    assert (
        customer_client.post(
            f"/api/v1/dev/payments/{other.pk}/simulate/", {"outcome": "fail"}, format="json"
        ).status_code
        == 404
    )


def _fresh(order: Order) -> None:
    """``place`` pins a past instant; move the reservation into the future so the order is payable now."""
    Order.objects.filter(pk=order.pk).update(
        created_at=timeutils.now(), reservation_expires_at=timeutils.now() + timedelta(minutes=15)
    )


@pytest.mark.parametrize("provider", ["click", "payme"])
def test_simulate_fail_then_success_pays_the_order(
    dev_routes: None, customer_client: APIClient, customer: Customer, provider: str
) -> None:
    """ "Payment failed, pay again": every run after the first uses a fresh provider transaction id."""
    seed_policies()
    order = place(customer, fulfilment="pickup", provider=provider)
    _fresh(order)
    url = f"/api/v1/dev/payments/{order.pk}/simulate/"
    failed = customer_client.post(url, {"outcome": "fail", "provider": provider}, format="json")
    assert failed.status_code == 200
    succeeded = customer_client.post(url, {"outcome": "success", "provider": provider}, format="json")
    assert succeeded.status_code == 200
    for step in succeeded.json()["steps"]:
        answer = step["response"]
        assert answer.get("error", 0) == 0 if provider == "click" else "error" not in answer, step
    order.refresh_from_db()
    assert order.status == "processing"
    attempts = list(Payment.objects.filter(order=order).order_by("id"))
    assert [p.status for p in attempts] == ["failed", "paid"]
    assert attempts[0].provider_txn_id != attempts[1].provider_txn_id
    if provider == "click":
        paydocs = {p.provider_state["click_paydoc_id"] for p in attempts}
        assert len(paydocs) == 2
