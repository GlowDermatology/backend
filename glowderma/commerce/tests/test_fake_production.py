"""Fake providers under the production settings (customer contract §6.14, plan §3 P2 tests): no free order, no
made-up delivery, no online selling without live Billz stock, and the open-money alarm at worker start."""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from typing import Any
from unittest import mock

import pytest
from django.test import override_settings
from rest_framework.test import APIClient

from glowderma.accounts import services as account_services
from glowderma.accounts.models import Customer
from glowderma.catalog.models import ProductStock
from glowderma.commerce import services, tasks
from glowderma.commerce.models import Cart, CartItem, Order, OrderEvent
from glowderma.commerce.tests.support import admin, deliver, pay, place, product, seed_policies, ship, shopper
from glowderma.commerce.tests.webhook_support import at, ms, payme, prepare, real_now, ref, tiyin
from glowderma.core.models import OutboxMessage
from glowderma.delivery.models import Shipment
from glowderma.integrations.bts import fake as fake_bts
from glowderma.integrations.bts.fake import FakeBts
from glowderma.integrations.payme import sandbox
from glowderma.loyalty.models import Purchase
from glowderma.payments.models import Payment

pytestmark = pytest.mark.django_db
MIXED = {"DEV_FEATURES_ALLOWED": False, "BTS_MODE": "fake", "BILLZ_MODE": "fake"}


def key() -> dict[str, Any]:
    return {"HTTP_IDEMPOTENCY_KEY": str(uuid.uuid4())}


def fill(customer: Customer) -> None:
    cart, _ = Cart.objects.get_or_create(customer=customer)
    CartItem.objects.create(cart=cart, product=product(), quantity=1)


def test_checkout_and_pay_refuse_a_fake_provider(customer_client: APIClient, customer: Customer) -> None:
    fill(customer)
    body = {"fulfilment": "pickup", "payment_provider": "click", "expected_total": 100_000}
    with override_settings(
        DEV_FEATURES_ALLOWED=False, CLICK_MODE="fake", PAYME_MODE="fake", BILLZ_MODE="live"
    ):
        response = customer_client.post("/api/v1/orders/checkout/", body, format="json", **key())
    assert response.status_code == 400 and response.json()["errors"][0]["field"] == "payment_provider"


def test_mixed_posture_delivery_is_not_offered(customer_client: APIClient, customer: Customer) -> None:
    fill(customer)
    # live Billz read the stock a moment ago: no stale-stock re-read (gap X-STOCK-FRESHNESS) at checkout
    ProductStock.objects.update(synced_at=real_now())
    own = account_services.add_address(customer.pk, text="Toshkent", region_code="10", city_code="1001")
    with (
        override_settings(DEV_FEATURES_ALLOWED=False, BTS_MODE="fake", BILLZ_MODE="live"),
        mock.patch("glowderma.core.integration_modes.payable_providers", return_value=["click"]),
        mock.patch.object(FakeBts, "quote") as quote,
    ):
        calculate = customer_client.post(
            "/api/v1/delivery/calculate/", {"fulfilment": "delivery", "address_id": own.pk}, format="json"
        )
        refused = customer_client.post(
            "/api/v1/orders/checkout/",
            {
                "fulfilment": "delivery",
                "address_id": own.pk,
                "payment_provider": "click",
                "expected_total": 125_000,
            },
            format="json",
            **key(),
        )
        pickup = customer_client.post(
            "/api/v1/orders/checkout/",
            {"fulfilment": "pickup", "payment_provider": "click", "expected_total": 100_000},
            format="json",
            **key(),
        )
        regions = customer_client.get("/api/v1/delivery/regions/").json()
    for response in (calculate, refused):
        assert response.status_code == 409 and response.json()["code"] == "delivery_unavailable"
        assert response.json()["message"] == "Yetkazib berish hozircha mavjud emas. Olib ketishni tanlang."
    quote.assert_not_called()
    assert pickup.status_code == 201, pickup.json()
    assert regions == {"regions": [], "updated_at": None}


def test_mixed_posture_shipments_and_polling() -> None:
    seed_policies()
    order = pay(place(fulfilment="delivery"))
    with override_settings(**MIXED):
        deliver("bts.create_shipment", order.pk)
        assert tasks.poll_bts() == []
    shipment = Shipment.objects.get(order=order)
    assert shipment.state == "failed" and shipment.tracking_number == "" and fake_bts._parcels == {}
    event = OrderEvent.objects.get(order=order, type="shipment_failed")
    assert event.data["error_code"] == "fake_mode"


def test_an_admin_cancel_of_a_created_parcel_while_bts_is_fake(staff_client: APIClient) -> None:
    seed_policies()
    order = ship(place(fulfilment="delivery"))
    with override_settings(**MIXED):
        response = staff_client.post(
            f"/api/v1/admin/orders/{order.pk}/cancel/", {"reason": "Mijoz so‘rovi"}, format="json", **key()
        )
    assert response.status_code == 503
    order.refresh_from_db()
    assert order.status == "processing"
    assert not OrderEvent.objects.filter(order=order, type="cancelled").exists()


def test_online_selling_off(customer_client: APIClient, customer: Customer) -> None:
    item = product(stock=5)
    fill(customer)
    line = CartItem.objects.get()
    with override_settings(**MIXED), mock.patch.object(FakeBts, "quote") as quote:
        for lang, text in (("uz", "Onlayn buyurtma"), ("ru", "Онлайн-заказ"), ("en", "Online ordering")):
            response = customer_client.post(
                "/api/v1/cart/items/",
                {"product_id": item.pk},
                format="json",
                HTTP_ACCEPT_LANGUAGE=lang,
                **key(),
            )
            assert response.status_code == 409 and response.json()["code"] == "online_sales_unavailable"
            assert response.json()["message"].startswith(text)
        patched = customer_client.patch(
            f"/api/v1/cart/items/{line.product_id}/", {"quantity": 2}, format="json"
        )
        cart = customer_client.get("/api/v1/cart/").json()
        checkout = customer_client.post(
            "/api/v1/orders/checkout/",
            {"fulfilment": "delivery", "address_id": 1, "payment_provider": "click", "expected_total": 1},
            format="json",
            **key(),
        )
    assert patched.status_code == 409 and patched.json()["code"] == "online_sales_unavailable"
    assert CartItem.objects.get().quantity == 1 and CartItem.objects.count() == 1
    assert cart["items"][0]["status"] == "out_of_stock" and cart["items"][0]["available"] == 0
    assert checkout.status_code == 409 and checkout.json()["code"] == "online_sales_unavailable"
    quote.assert_not_called()


def test_an_earlier_order_can_still_be_paid_and_its_sale_is_not_pushed(
    customer_client: APIClient, customer: Customer, caplog: pytest.LogCaptureFixture
) -> None:
    seed_policies()
    order = place(customer, fulfilment="pickup", when=real_now())
    with (
        override_settings(DEV_FEATURES_ALLOWED=False, BILLZ_MODE="fake"),
        mock.patch("glowderma.core.integration_modes.payable_providers", return_value=["click"]),
    ):
        response = customer_client.post(f"/api/v1/orders/{order.pk}/pay/", {}, format="json", **key())
        assert response.status_code == 200
        pay(order, real_now() + timedelta(seconds=10))
        with caplog.at_level(logging.WARNING):
            deliver("billz.push_sale", order.pk)
    purchase = Purchase.objects.get(order_id=order.pk)
    assert purchase.billz_pushed_at is None
    assert not OrderEvent.objects.filter(order=order, type="billz_sale_recorded").exists()
    assert "billz.sale_not_pushed" in caplog.text


def test_open_money_at_worker_start(caplog: pytest.LogCaptureFixture) -> None:
    seed_policies()
    now = real_now()
    click_order = place(shopper(), fulfilment="pickup", when=now)
    with at(now + timedelta(seconds=10)):
        assert prepare(click_order)["error"] == 0
    payme_order = place(shopper(), fulfilment="pickup", provider="payme", when=now)
    with at(now + timedelta(seconds=10)):
        payme(sandbox.create("payme-open", ref(payme_order), tiyin(payme_order), ms(now)))
        payme(sandbox.perform("payme-open"))
    services.cancel_order(payme_order.pk, "Mijoz so‘rovi", admin().pk)  # a manual_required Payme refund
    messages = OutboxMessage.objects.count()
    with caplog.at_level(logging.ERROR, logger="glowderma.commerce.tasks"):
        assert tasks.report_fake_provider_open_money() == []  # local/test settings: nothing
        with override_settings(DEV_FEATURES_ALLOWED=False, CLICK_MODE="fake", PAYME_MODE="fake"):
            assert tasks.report_fake_provider_open_money() == ["click", "payme"]
    records = [r.getMessage() for r in caplog.records if "providers.fake_with_open_money" in r.getMessage()]
    assert len(records) == 2
    assert f"'order_ids': [{click_order.pk}]" in records[0] and "'open_transactions': 1" in records[0]
    assert f"'order_ids': [{payme_order.pk}]" in records[1] and "'open_refunds': 1" in records[1]
    assert OutboxMessage.objects.count() == messages
    assert Payment.objects.filter(order=click_order).get().status == "pending"


def test_open_money_at_worker_start_raises_one_alert_per_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    from glowderma.commerce import selectors

    held = {
        "click": selectors.OpenMoney(open_transactions=1, open_refunds=0, order_ids=[10248]),
        "payme": selectors.OpenMoney(open_transactions=0, open_refunds=2, order_ids=[10249, 10250]),
    }
    monkeypatch.setattr(selectors, "provider_open_money", lambda provider: held[str(provider)])
    with override_settings(
        DEV_FEATURES_ALLOWED=False,
        CLICK_MODE="fake",
        PAYME_MODE="fake",
        ALERT_WEBHOOK_URL="https://alerts.example.uz/hook",
    ):
        assert tasks.report_fake_provider_open_money() == ["click", "payme"]
    alerts = OutboxMessage.objects.filter(topic="alert.send").order_by("id")
    assert [(a.payload["event"], a.payload["fields"]) for a in alerts] == [
        (
            "providers.fake_with_open_money",
            {"provider": "click", "open_transactions": 1, "open_refunds": 0, "order_ids": [10248]},
        ),
        (
            "providers.fake_with_open_money",
            {"provider": "payme", "open_transactions": 0, "open_refunds": 2, "order_ids": [10249, 10250]},
        ),
    ]


def test_no_open_money_no_alarm(caplog: pytest.LogCaptureFixture) -> None:
    with override_settings(DEV_FEATURES_ALLOWED=False, CLICK_MODE="fake", PAYME_MODE="fake"):
        with caplog.at_level(logging.ERROR):
            assert tasks.report_fake_provider_open_money() == []
    assert "providers.fake_with_open_money" not in caplog.text


def test_the_worker_ready_hook_never_raises() -> None:
    with mock.patch.object(tasks, "report_fake_provider_open_money", side_effect=RuntimeError("db down")):
        tasks._report_open_money_at_worker_start()
    assert Order.objects.count() == 0
