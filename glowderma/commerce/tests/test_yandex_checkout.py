"""``POST orders/checkout/`` with ``delivery_quote_id`` (plan §3.4): the fee comes from the stored quote, the
pin is snapshotted, the shipment is Yandex's, the quote is used once; a stale quote is re-quoted (same fee →
proceed, other fee → 409 ``price_changed``, nothing → 409 ``quote_expired``); payment needs nothing new."""

# ruff: noqa: F811 - the fixtures come from yandex_checkout_support and are requested by name

from __future__ import annotations

import threading
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.db import connection
from rest_framework.test import APIClient

from glowderma.accounts import services as account_services
from glowderma.accounts.models import Address, Customer
from glowderma.accounts.tests.factories import CustomerFactory
from glowderma.commerce import quoting, services, topics
from glowderma.commerce.models import Order
from glowderma.commerce.tests.support import credit, pay, product, seed_policies, shopper
from glowderma.commerce.tests.webhook_support import real_now
from glowderma.commerce.tests.yandex_checkout_support import (  # noqa: F401 - fixtures
    CUSTOMER_PIN,
    calculate,
    checkout,
    configure_warehouse,
    expected_fee,
    fill,
    fresh_fake_yandex,
    frozen,
    pinned_address,
    price_calls,
    scenario_header,
    yandex_body,
)
from glowderma.core import enums, timeutils
from glowderma.core.models import OutboxMessage
from glowderma.delivery.models import DeliveryQuote, DeliverySettings, Shipment
from glowderma.integrations.bts.fake import FakeBts

pytestmark = pytest.mark.django_db
DETAIL_KEYS_BEFORE = {
    "id",
    "created_at",
    "status",
    "status_label",
    "fulfilment",
    "total",
    "item_count",
    "preview_images",
    "preview_thumbnail_images",
    "payment",
    "pay_by",
    "tracking_number",
    "fulfilment_label",
    "items",
    "items_subtotal",
    "promo_subtotal",
    "delivery_fee",
    "cashback_used",
    "address",
    "address_comment",
    "pickup_address",
    "customer_note",
    "refund",
    "shipment",
    "cashback",
    "cancellation",
    "progress",
    "timeline",
    "actions",
}


def _quoted(
    client: APIClient, customer: Customer, *, method: str | None = None, region: str = "10", **warehouse: Any
) -> tuple[Address, dict[str, Any]]:
    configure_warehouse(**warehouse)
    fill(customer, (product(price=300_000), 1))
    address = pinned_address(customer, region=region, city="1001" if region else "")
    preference: dict[str, Any] = {"delivery_method": method} if method else {}
    response = calculate(client, address, **preference)
    assert response.status_code == 200, response.json()
    return address, response.json()


def _expire(quote_id: str) -> None:
    now = timeutils.now()
    DeliveryQuote.objects.filter(pk=quote_id).update(
        created_at=now - timedelta(minutes=20), expires_at=now - timedelta(minutes=5)
    )


def test_standard_checkout_uses_the_quote(
    customer_client: APIClient, customer: Customer, frozen: Any, price_calls: list[str]
) -> None:
    address, quote = _quoted(customer_client, customer)
    calls_after_quote = len(price_calls)
    response = checkout(customer_client, yandex_body(address, quote))
    assert response.status_code == 201, response.json()
    assert len(price_calls) == calls_after_quote  # a good quote is not asked again
    order = Order.objects.get(pk=response.json()["id"])
    fee = expected_fee("courier")
    assert quote["delivery_fee"] == fee and order.delivery_fee == fee and order.total == 300_000 + fee
    assert (order.address_latitude, order.address_longitude) == CUSTOMER_PIN
    shipment = Shipment.objects.get(order=order)
    assert (shipment.provider, shipment.method, str(shipment.quote_id)) == (
        "yandex",
        "standard",
        quote["quote_id"],
    )
    stored = DeliveryQuote.objects.get(pk=quote["quote_id"])
    assert stored.used_at == frozen and stored.order_id == order.pk
    body = response.json()
    assert set(body) == DETAIL_KEYS_BEFORE | {"delivery"}
    assert body["delivery"]["provider"] == "yandex" and body["delivery"]["method"] == "standard"
    assert body["delivery"]["method_label"] == "Standart" and body["delivery"]["stage"] is None
    assert body["delivery"]["eta_from"] == quote["options"][0]["eta_from"]
    assert body["shipment"] is None and body["tracking_number"] is None
    assert not OutboxMessage.objects.filter(topic=topics.BTS_CREATE_SHIPMENT).exists()


def test_express_checkout_without_bts_codes(
    customer_client: APIClient, customer: Customer, frozen: Any
) -> None:
    address, quote = _quoted(customer_client, customer, method="express", region="")
    assert quote["method"] == "express" and quote["region_code"] is None
    response = checkout(customer_client, yandex_body(address, quote))
    assert response.status_code == 201, response.json()
    order = Order.objects.get(pk=response.json()["id"])
    assert order.delivery_fee == expected_fee("express")
    assert (order.address_region_code, order.address_city_code) == ("", "")
    assert Shipment.objects.get(order=order).method == "express"


def test_an_expired_quote_with_the_same_fee_proceeds_with_a_fresh_one(
    customer_client: APIClient, customer: Customer, frozen: Any, price_calls: list[str]
) -> None:
    address, quote = _quoted(customer_client, customer)
    _expire(quote["quote_id"])
    response = checkout(customer_client, yandex_body(address, quote))
    assert response.status_code == 201, response.json()
    order = Order.objects.get(pk=response.json()["id"])
    shipment = Shipment.objects.get(order=order)
    assert str(shipment.quote_id) != quote["quote_id"] and order.delivery_fee == quote["delivery_fee"]
    assert DeliveryQuote.objects.get(pk=quote["quote_id"]).used_at is None
    assert DeliveryQuote.objects.get(pk=str(shipment.quote_id)).order_id == order.pk


def test_an_expired_quote_with_another_fee_is_price_changed(
    customer_client: APIClient, customer: Customer, frozen: Any
) -> None:
    address, quote = _quoted(customer_client, customer)
    _expire(quote["quote_id"])
    response = checkout(
        customer_client, yandex_body(address, quote), header=scenario_header({"price": 99_000})
    )
    assert response.status_code == 409 and response.json()["code"] == "price_changed"
    details = response.json()["details"]
    fresh_fee = expected_fee("courier", price=99_000)
    assert details["delivery_fee"] == fresh_fee and details["total"] == 300_000 + fresh_fee
    assert details["items_subtotal"] == 300_000 and details["cashback_used"] == 0 and details["cart"]["items"]
    fresh = next(o for o in details["options"] if o["method"] == "standard")
    assert fresh["fee"] == fresh_fee and fresh["quote_id"] == details["quote_id"] != quote["quote_id"]
    assert response.json()["message"] == "Yetkazish narxi o‘zgardi. Yangi narxni tekshiring."
    assert not Order.objects.exists()
    # the customer accepts the new price
    retry = checkout(customer_client, yandex_body(address, fresh), header=scenario_header({"price": 99_000}))
    assert retry.status_code == 201, retry.json()
    assert Order.objects.get().delivery_fee == fresh_fee


def test_no_fresh_quote_is_quote_expired(customer_client: APIClient, customer: Customer, frozen: Any) -> None:
    address, quote = _quoted(customer_client, customer)
    _expire(quote["quote_id"])
    response = checkout(
        customer_client, yandex_body(address, quote), header=scenario_header({"check_price": "down"})
    )
    assert response.status_code == 409 and response.json()["code"] == "quote_expired"
    reasons = {o["method"]: o["unavailable_reason"] for o in response.json()["details"]["options"]}
    # the express quote of the calculate is still valid and reused; only standard needed Yandex
    assert reasons == {"standard": "provider_unavailable", "express": None, None: None}
    assert not Order.objects.exists()


def test_a_quote_of_another_customer_is_quote_expired(
    customer_client: APIClient, customer: Customer, price_calls: list[str]
) -> None:
    configure_warehouse()
    other = shopper()
    fill(other, (product(), 1))
    from glowderma.delivery.tests.factories import DeliveryQuoteFactory

    foreign = DeliveryQuoteFactory.create(customer=other)
    fill(customer, (product(price=300_000), 1))
    address = pinned_address(customer)
    body = yandex_body(
        address, {"quote_id": str(foreign.pk), "expected_total": 300_000 + foreign.customer_fee}
    )
    response = checkout(customer_client, body)
    assert response.status_code == 409 and response.json()["code"] == "quote_expired"
    assert response.json()["details"] is None and price_calls == []
    unknown = checkout(customer_client, {**body, "delivery_quote_id": "7f9c4a7e-2b54-4c3f-9f3c-0d7b5f8e1a11"})
    assert unknown.status_code == 409 and unknown.json()["code"] == "quote_expired"


def test_a_changed_address_text_is_re_quoted(
    customer_client: APIClient, customer: Customer, frozen: Any
) -> None:
    address, quote = _quoted(customer_client, customer)
    account_services.update_address(address, text="Toshkent, Chilonzor tumani, 9-kvartal, 14-uy")
    response = checkout(customer_client, yandex_body(address, quote))
    assert response.status_code == 201, response.json()  # same pin → same fee
    order = Order.objects.get(pk=response.json()["id"])
    fresh = DeliveryQuote.objects.get(pk=str(Shipment.objects.get(order=order).quote_id))
    assert str(fresh.pk) != quote["quote_id"] and fresh.address_text == order.address_text
    assert order.address_text == "Toshkent, Chilonzor tumani, 9-kvartal, 14-uy"


def test_a_moved_pin_is_price_changed(customer_client: APIClient, customer: Customer, frozen: Any) -> None:
    address, quote = _quoted(customer_client, customer)
    far = (Decimal("41.360000"), Decimal("69.330000"))
    account_services.update_address(address, latitude=far[0], longitude=far[1])
    response = checkout(customer_client, yandex_body(address, quote))
    assert response.status_code == 409 and response.json()["code"] == "price_changed"
    assert response.json()["details"]["delivery_fee"] == expected_fee("courier", pin=far)


def test_a_disabled_or_suspended_method_is_quote_expired(
    customer_client: APIClient, customer: Customer, frozen: Any
) -> None:
    address, quote = _quoted(customer_client, customer)
    DeliverySettings.objects.filter(pk=1).update(standard_enabled=False)
    response = checkout(customer_client, yandex_body(address, quote))
    assert response.status_code == 409 and response.json()["code"] == "quote_expired"
    assert [o["method"] for o in response.json()["details"]["options"]] == ["express", None]
    DeliverySettings.objects.filter(pk=1).update(standard_enabled=True, dispatch_suspended=True)
    suspended = checkout(customer_client, yandex_body(address, quote))
    assert suspended.status_code == 409 and suspended.json()["code"] == "quote_expired"


def test_pickup_refuses_a_quote_and_bts_checkout_is_unchanged(
    customer_client: APIClient, customer: Customer, frozen: Any
) -> None:
    address, quote = _quoted(customer_client, customer)
    pickup = checkout(
        customer_client,
        {
            "fulfilment": "pickup",
            "payment_provider": "click",
            "expected_total": 300_000,
            "delivery_quote_id": quote["quote_id"],
        },
    )
    assert pickup.status_code == 400 and pickup.json()["errors"][0]["field"] == "delivery_quote_id"
    fee = FakeBts().quote("10", "1001")
    legacy = checkout(
        customer_client,
        {
            "fulfilment": "delivery",
            "address_id": address.pk,
            "payment_provider": "click",
            "expected_total": 300_000 + fee,
        },
    )
    assert legacy.status_code == 201, legacy.json()
    order = Order.objects.get(pk=legacy.json()["id"])
    assert order.delivery_fee == fee and order.address_latitude is None
    shipment = Shipment.objects.get(order=order)
    assert (shipment.provider, shipment.method, shipment.quote_id) == ("bts", "", None)
    assert legacy.json()["delivery"] == {
        "provider": "bts",
        "method": None,
        "method_label": None,
        "stage": None,
        "stage_label": None,
        "eta_from": None,
        "eta_to": None,
        "courier_name": None,
        "courier_vehicle": None,
        "tracking_url": None,
    }
    assert DeliveryQuote.objects.filter(used_at__isnull=False).count() == 0


def _input(address: Address, quote: dict[str, Any]) -> services.CheckoutInput:
    return services.CheckoutInput(
        fulfilment="delivery",
        payment_provider="click",
        channel="ios",
        expected_total=quote["expected_total"],
        address_id=address.pk,
        delivery_quote_id=quote["quote_id"],
    )


def test_a_used_quote_cannot_pay_for_a_second_order(
    customer_client: APIClient, customer: Customer, frozen: Any
) -> None:
    address, quote = _quoted(customer_client, customer)
    from uuid import UUID

    quote = {**quote, "quote_id": UUID(quote["quote_id"])}
    services.checkout(customer.pk, _input(address, quote))
    fill(customer, (product(price=300_000), 1))
    with pytest.raises(quoting.QuoteExpired):
        services.checkout(customer.pk, _input(address, quote))
    assert Order.objects.count() == 1


def test_a_changed_cart_weight_is_refused_in_the_transaction(
    customer_client: APIClient, customer: Customer, frozen: Any
) -> None:
    from uuid import UUID

    address, quote = _quoted(customer_client, customer)
    fill(customer, (product(price=1), 1))  # one more unit → another parcel weight
    with pytest.raises(quoting.QuoteExpired):
        services.checkout(customer.pk, _input(address, {**quote, "quote_id": UUID(quote["quote_id"])}))


@pytest.mark.django_db(transaction=True)
def test_two_checkouts_race_for_one_quote() -> None:
    """Both requests passed the quote check of ``post()``; the customer lock serialises the transactions and
    the loser finds the quote used → ``quote_expired``, never a second order on one quote."""
    from uuid import UUID

    configure_warehouse()
    customer = CustomerFactory.create()
    fill(customer, (product(price=300_000), 1))
    address = pinned_address(customer)
    request = quoting.QuoteRequest(
        customer_id=customer.pk,
        address=quoting.AddressSnapshot(
            address.pk,
            address.text,
            address.latitude,
            address.longitude,
            address.region_code,
            address.city_code,
        ),
        quantity=1,
        items_subtotal=300_000,
        cashback_available=0,
        cashback_to_use=0,
        lang="uz",
        now=timeutils.now(),
    )
    standard = quoting.delivery_options(request)[0]
    assert standard.quote_id is not None and standard.expected_total is not None
    quote: dict[str, Any] = {
        "quote_id": UUID(str(standard.quote_id)),
        "expected_total": standard.expected_total,
    }
    start = threading.Barrier(2, timeout=10)
    results: list[Any] = []

    def run() -> None:
        try:
            start.wait()
            results.append(services.checkout(customer.pk, _input(address, quote)))
        except Exception as exc:  # noqa: BLE001 - the loser's error is the assertion
            results.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    orders = [r for r in results if isinstance(r, int)]
    lost = [r for r in results if isinstance(r, quoting.QuoteExpired)]
    assert len(orders) == 1 and len(lost) == 1, results
    assert DeliveryQuote.objects.get(pk=str(quote["quote_id"])).order_id == orders[0]


def test_yandex_checkout_then_payment(customer_client: APIClient, customer: Customer) -> None:
    """Plan §3.5: one Click payment for products + fee − cashback; the order is processing, the shipment
    awaits dispatch, nothing goes to BTS."""
    seed_policies()
    credit(customer, 100_000, real_now())
    configure_warehouse()
    fill(customer, (product(price=300_000), 1))
    address = pinned_address(customer)
    quote = calculate(customer_client, address, cashback_to_use=5_000).json()
    assert quote["cashback_to_use"] == 5_000
    response = checkout(customer_client, yandex_body(address, quote, cashback_to_use=5_000))
    assert response.status_code == 201, response.json()
    order = Order.objects.get(pk=response.json()["id"])
    assert order.total == order.items_subtotal + order.delivery_fee - order.cashback_used
    assert (order.items_subtotal, order.cashback_used) == (300_000, 5_000)
    assert order.delivery_fee == quote["delivery_fee"] > 0
    paid = pay(order)
    assert paid.status == enums.OrderStatus.PROCESSING
    shipment = Shipment.objects.get(order=order)
    assert shipment.provider == "yandex" and shipment.stage == enums.ShipmentStage.AWAITING_DISPATCH
    assert not OutboxMessage.objects.filter(
        topic=topics.BTS_CREATE_SHIPMENT, payload__order_id=order.pk
    ).exists()
    detail = customer_client.get(f"/api/v1/me/orders/{order.pk}/").json()
    assert detail["delivery"]["stage"] == "awaiting_dispatch"
    assert detail["delivery"]["stage_label"] == "Jo‘natishga tayyorlanmoqda"
    assert detail["payment"]["amount"] == order.total
