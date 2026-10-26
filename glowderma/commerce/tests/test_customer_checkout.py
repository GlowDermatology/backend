"""``delivery/calculate/`` and ``orders/checkout/`` (customer contract §2.6, §2.7.1): every check row in order,
the snapshots, the replay, and the rule that no BTS call runs while a row lock is held."""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest import mock

import pytest
from django.test import override_settings
from rest_framework.test import APIClient

from glowderma.accounts import services as account_services
from glowderma.accounts.models import Address, Customer
from glowderma.catalog.models import Category
from glowderma.commerce import services
from glowderma.commerce.models import Cart, CartItem, Order, OrderEvent
from glowderma.commerce.tests.support import credit, place, product, seed_policies, shopper
from glowderma.commerce.tests.webhook_support import real_now
from glowderma.integrations.bts.fake import FakeBts
from glowderma.site_config.models import SiteSettings

pytestmark = pytest.mark.django_db
CHECKOUT = "/api/v1/orders/checkout/"
CALCULATE = "/api/v1/delivery/calculate/"
SCENARIO = {"FAKE_SCENARIO_HEADER_ENABLED": True}


def key() -> dict[str, str]:
    return {"HTTP_IDEMPOTENCY_KEY": str(uuid.uuid4())}


def fill(customer: Customer, *lines: tuple[Any, int]) -> None:
    cart, _ = Cart.objects.get_or_create(customer=customer)
    for item, quantity in lines:
        CartItem.objects.create(cart=cart, product=item, quantity=quantity, price_at_add=item.price)


def address(customer: Customer, region: str = "10", city: str = "1001") -> Address:
    return account_services.add_address(
        customer.pk, text="Toshkent, Chilonzor tumani, 9-kvartal", region_code=region, city_code=city
    )


def checkout(client: APIClient, body: dict[str, Any], **extra: Any) -> Any:
    return client.post(CHECKOUT, body, format="json", **{**key(), **extra})


def pickup_body(total: int, **over: Any) -> dict[str, Any]:
    return {"fulfilment": "pickup", "address_id": None, "payment_provider": "click", "expected_total": total, **over}


# --- delivery/calculate/ ----------------------------------------------------------------------------------------


def test_calculate_delivery_and_pickup(customer_client: APIClient, customer: Customer) -> None:
    seed_policies()
    credit(customer, 100_000, real_now())
    fill(customer, (product(price=300_000), 2))
    own = address(customer, "11", "1101")
    body = customer_client.post(
        CALCULATE, {"fulfilment": "delivery", "address_id": own.pk, "cashback_to_use": 10**6}, format="json"
    ).json()
    assert body["delivery_fee"] == 26_000 and body["region_code"] == "11" and body["city_code"] == "1101"
    assert body["weight_g"] == 1000 and body["pickup_address"] is None and body["items_subtotal"] == 600_000
    assert body["cashback_max"] == 100_000  # min(available, subtotal, subtotal + fee − 1 000)
    assert body["cashback_to_use"] == body["cashback_max"]
    assert body["expected_total"] == 600_000 + 26_000 - body["cashback_max"]
    SiteSettings.objects.filter(pk=SiteSettings.load().pk).update(pickup_address_uz="Toshkent, Yunusobod")
    pickup = customer_client.post(CALCULATE, {"fulfilment": "pickup"}, format="json").json()
    assert pickup["delivery_fee"] == 0 and pickup["pickup_address"] == "Toshkent, Yunusobod"
    assert pickup["cashback_to_use"] == 0 and pickup["expected_total"] == 600_000 and pickup["weight_g"] is None
    pair = customer_client.post(
        CALCULATE, {"fulfilment": "delivery", "region_code": "13", "city_code": "1301"}, format="json"
    ).json()
    assert pair["delivery_fee"] == 28_000


def test_calculate_address_errors(customer_client: APIClient, customer: Customer) -> None:
    fill(customer, (product(), 1))
    missing = customer_client.post(CALCULATE, {"fulfilment": "delivery"}, format="json").json()
    assert missing["errors"][0] == {
        "field": "address_id",
        "code": "required",
        "message": "Yetkazib berish manzilini tanlang.",
    }
    foreign = address(shopper())
    other = customer_client.post(CALCULATE, {"fulfilment": "delivery", "address_id": foreign.pk}, format="json")
    assert other.json()["errors"][0]["code"] == "does_not_exist"
    assert other.json()["errors"][0]["message"] == "Manzil topilmadi."
    unknown = address(customer, "77", "7701")
    bad = customer_client.post(CALCULATE, {"fulfilment": "delivery", "address_id": unknown.pk}, format="json")
    assert bad.json()["errors"][0]["code"] == "address_incomplete"
    assert customer_client.post(CALCULATE, {"fulfilment": "drone"}, format="json").json()["errors"][0]["code"] == (
        "invalid_choice"
    )


def test_calculate_bts_refusals_and_empty_cart(customer_client: APIClient, customer: Customer) -> None:
    own = address(customer)
    empty = customer_client.post(CALCULATE, {"fulfilment": "pickup"}, format="json")
    assert empty.status_code == 409 and empty.json()["code"] == "cart_empty"
    fill(customer, (product(), 1))
    body = {"fulfilment": "delivery", "address_id": own.pk}
    with override_settings(**SCENARIO):
        rejected = customer_client.post(
            CALCULATE, body, format="json", HTTP_X_GD_FAKE_SCENARIO=json.dumps({"bts": {"quote": "rejected"}})
        )
        down = customer_client.post(
            CALCULATE, body, format="json", HTTP_X_GD_FAKE_SCENARIO=json.dumps({"bts": {"quote": "down"}})
        )
        fixed = customer_client.post(
            CALCULATE, body, format="json", HTTP_X_GD_FAKE_SCENARIO=json.dumps({"bts": {"tariff": 31_000}})
        )
    assert rejected.status_code == 409 and rejected.json()["code"] == "delivery_unavailable"
    assert rejected.json()["message"] == "Bu hududga yetkazib berish mavjud emas."
    assert down.status_code == 503 and down.json()["code"] == "provider_unavailable"
    assert fixed.json()["delivery_fee"] == 31_000
    ignored = customer_client.post(
        CALCULATE, body, format="json", HTTP_X_GD_FAKE_SCENARIO=json.dumps({"bts": {"quote": "rejected"}})
    )
    assert ignored.status_code == 200  # the header is honoured only with FAKE_SCENARIO_HEADER_ENABLED


def test_calculate_numbers_equal_checkout(customer_client: APIClient, customer: Customer) -> None:
    seed_policies()
    credit(customer, 5_000_000, real_now())
    fill(customer, (product(price=150_000), 1))
    own = address(customer)
    quote = customer_client.post(
        CALCULATE, {"fulfilment": "delivery", "address_id": own.pk, "cashback_to_use": 10**7}, format="json"
    ).json()
    created = checkout(
        customer_client,
        {
            "fulfilment": "delivery",
            "address_id": own.pk,
            "payment_provider": "click",
            "cashback_to_use": quote["cashback_to_use"],
            "expected_total": quote["expected_total"],
        },
    )
    assert created.status_code == 201, created.json()
    order = Order.objects.get(pk=created.json()["id"])
    assert (order.total, order.cashback_used, order.delivery_fee) == (
        quote["expected_total"],
        quote["cashback_to_use"],
        quote["delivery_fee"],
    )
    assert order.total == order.delivery_fee  # cashback covers the items, never the delivery (C-4)


# --- checkout ---------------------------------------------------------------------------------------------------


def test_checkout_creates_the_order_and_empties_the_cart(customer_client: APIClient, customer: Customer) -> None:
    cream = product(price=312_000, stock=5, image=True)
    spf = product(price=240_000, stock=5, promo=True)
    fill(customer, (cream, 1), (spf, 2))
    own = address(customer)
    total = 312_000 + 480_000 + 25_000
    headers = key()
    body = {
        "fulfilment": "delivery",
        "address_id": own.pk,
        "payment_provider": "click",
        "expected_total": total,
        "customer_note": "Soat 18:00 dan keyin",
    }
    response = customer_client.post(CHECKOUT, body, format="json", **headers)
    assert response.status_code == 201, response.json()
    detail = response.json()
    assert detail["status"] == "awaiting_payment" and detail["actions"] == {"pay": True, "cancel": True, "reorder": False}
    assert detail["pay_by"] is not None and detail["customer_note"] == "Soat 18:00 dan keyin"
    assert detail["items"][0]["image_url"].startswith("http://127.0.0.1:8000/media/products/")
    order = Order.objects.get(pk=detail["id"])
    assert order.customer_note == "Soat 18:00 dan keyin" and order.channel == "ios"
    assert order.metadata["checkout"]["app_version"] == "1.0.0" and order.metadata["checkout"]["device_id"]
    assert order.items.get(position=0).image_url == cream.images.get().file.name
    assert [e.type for e in OrderEvent.objects.filter(order=order).order_by("id")] == [
        "order_created",
        "reservation_created",
    ]
    assert not CartItem.objects.filter(cart__customer=customer).exists()
    assert Cart.objects.get(customer=customer).last_checkout_order_id == order.pk
    replay = customer_client.post(CHECKOUT, body, format="json", **headers)
    assert replay.status_code == 201 and replay.json()["id"] == order.pk
    assert Order.objects.filter(customer=customer).count() == 1


def test_checkout_row_1_body(customer_client: APIClient, customer: Customer) -> None:
    fill(customer, (product(), 1))

    def errors(body: dict[str, Any]) -> list[tuple[str, str]]:
        response = checkout(customer_client, body)
        assert response.status_code == 400, response.json()
        return [(item["field"], item["code"]) for item in response.json()["errors"]]

    assert ("fulfilment", "invalid_choice") in errors(pickup_body(1, fulfilment="drone"))
    assert ("payment_provider", "invalid_choice") in errors(pickup_body(1, payment_provider="cash"))
    assert errors({"fulfilment": "delivery", "payment_provider": "click", "expected_total": 1}) == [
        ("address_id", "required")
    ]
    assert errors(pickup_body(1, address_id=address(customer).pk)) == [("address_id", "invalid")]
    assert ("cashback_to_use", "min_value") in errors(pickup_body(1, cashback_to_use=-1))
    assert ("expected_total", "required") in errors({"fulfilment": "pickup", "payment_provider": "click"})
    assert ("customer_note", "max_length") in errors(pickup_body(1, customer_note="x" * 301))
    with mock.patch("glowderma.core.integration_modes.payable_providers", return_value=["payme"]):
        assert errors(pickup_body(1)) == [("payment_provider", "invalid_choice")]


def test_checkout_row_2_consent(customer_client: APIClient, customer: Customer) -> None:
    fill(customer, (product(), 1))
    SiteSettings.objects.filter(pk=SiteSettings.load().pk).update(terms_version="2026-10-01")
    response = checkout(customer_client, pickup_body(100_000))
    assert response.status_code == 409 and response.json()["code"] == "terms_update_required"
    assert response.json()["details"] == {"current_version": "2026-10-01"}


def test_checkout_rows_3_4_delivery(customer_client: APIClient, customer: Customer) -> None:
    fill(customer, (product(), 1))
    foreign = address(shopper())
    body = {"fulfilment": "delivery", "payment_provider": "click", "expected_total": 125_000}
    assert checkout(customer_client, {**body, "address_id": foreign.pk}).json()["errors"][0]["code"] == "does_not_exist"
    unknown = address(customer, "77", "7701")
    assert checkout(customer_client, {**body, "address_id": unknown.pk}).json()["errors"][0]["code"] == (
        "address_incomplete"
    )
    own = address(customer)
    with override_settings(**SCENARIO):
        rejected = checkout(
            customer_client,
            {**body, "address_id": own.pk},
            HTTP_X_GD_FAKE_SCENARIO=json.dumps({"bts": {"quote": "rejected"}}),
        )
        down = checkout(
            customer_client, {**body, "address_id": own.pk}, HTTP_X_GD_FAKE_SCENARIO=json.dumps({"bts": {"quote": "down"}})
        )
    assert rejected.status_code == 409 and rejected.json()["code"] == "delivery_unavailable"
    assert down.status_code == 503
    assert not Order.objects.exists()


def test_checkout_rows_5_6_open_order_and_empty_cart(customer_client: APIClient, customer: Customer) -> None:
    empty = checkout(customer_client, pickup_body(100_000))
    assert empty.status_code == 409 and empty.json()["code"] == "cart_empty"
    open_order = place(customer, fulfilment="pickup")
    fill(customer, (product(), 1))
    response = checkout(customer_client, pickup_body(100_000))
    assert response.status_code == 409 and response.json()["code"] == "open_order_exists"
    assert response.json()["details"]["order_id"] == open_order.pk and response.json()["details"]["pay_by"]
    assert f"№{open_order.pk}" in response.json()["message"]
    with override_settings(CHECKOUT_MAX_OPEN_ORDERS=2):
        assert checkout(customer_client, pickup_body(100_000)).status_code == 201


def test_checkout_row_7_out_of_stock_lines(customer_client: APIClient, customer: Customer) -> None:
    hidden = product(stock=5, name_ru="Скрытый")
    scarce = product(stock=1)
    gone = product(stock=0)
    fine = product(stock=9)
    fill(customer, (hidden, 1), (scarce, 3), (gone, 1), (fine, 1))
    Category.objects.filter(pk=hidden.category_id).update(is_active=False)
    response = checkout(customer_client, pickup_body(1), HTTP_ACCEPT_LANGUAGE="ru")
    assert response.status_code == 409 and response.json()["code"] == "OUT_OF_STOCK"
    assert response.json()["message"] == "Некоторых товаров недостаточно."
    lines = response.json()["details"]["lines"]
    assert [(line["product_id"], line["status"], line["requested"], line["available"]) for line in lines] == [
        (hidden.pk, "unavailable", 1, 5),
        (scarce.pk, "insufficient", 3, 1),
        (gone.pk, "out_of_stock", 1, 0),
    ]
    assert lines[0]["name"] == "Скрытый"


def test_checkout_rows_8_9_10_cashback_and_price(customer_client: APIClient, customer: Customer) -> None:
    seed_policies()
    credit(customer, 10_000, real_now())  # 10 000 so'm spendable
    item = product(price=10_000, stock=9)
    fill(customer, (item, 1))
    above = checkout(customer_client, pickup_body(1_000, cashback_to_use=9_001))
    assert above.status_code == 400
    assert above.json()["errors"][0] == {
        "field": "cashback_to_use",
        "code": "max_value",
        "message": "Ko‘pi bilan 9\u00a0000 so‘m cashback ishlatish mumkin.",
    }
    changed = checkout(customer_client, pickup_body(9_999))
    assert changed.status_code == 409 and changed.json()["code"] == "price_changed"
    details = changed.json()["details"]
    assert (details["total"], details["items_subtotal"], details["delivery_fee"], details["cashback_used"]) == (
        10_000,
        10_000,
        0,
        0,
    )
    assert details["cart"]["items"][0]["product_id"] == item.pk
    expensive = product(price=50_000, stock=9)
    fill(customer, (expensive, 1))
    short = checkout(customer_client, pickup_body(60_000 - 20_000, cashback_to_use=20_000))
    assert short.status_code == 400
    assert short.json()["errors"][0]["code"] == "insufficient_balance"
    assert short.json()["errors"][0]["message"] == "Cashback balansi yetarli emas (mavjud: 10\u00a0000 so‘m)."
    assert not Order.objects.exists()


def test_checkout_row_0_online_sales_off(customer_client: APIClient, customer: Customer) -> None:
    fill(customer, (product(), 1))
    with override_settings(DEV_FEATURES_ALLOWED=False, BILLZ_MODE="fake"), mock.patch.object(FakeBts, "quote") as quote:
        response = checkout(customer_client, {"fulfilment": "drone"}, HTTP_ACCEPT_LANGUAGE="en")
    assert response.status_code == 409 and response.json()["code"] == "online_sales_unavailable"
    assert response.json()["message"].startswith("Online ordering is temporarily unavailable")
    quote.assert_not_called()


def test_no_bts_call_while_a_row_lock_is_held(customer_client: APIClient, customer: Customer) -> None:
    fill(customer, (product(), 1))
    own = address(customer)
    calls: list[str] = []
    real_lock = services.lock_customer
    real_quote = FakeBts.quote

    def lock(customer_id: int) -> Any:
        calls.append("lock")
        return real_lock(customer_id)

    def quote(self: FakeBts, region: str, city: str) -> int:
        calls.append("quote")
        return real_quote(self, region, city)

    with mock.patch.object(services, "lock_customer", lock), mock.patch.object(FakeBts, "quote", quote):
        response = checkout(
            customer_client,
            {"fulfilment": "delivery", "address_id": own.pk, "payment_provider": "payme", "expected_total": 125_000},
        )
    assert response.status_code == 201, response.json()
    assert calls == ["quote", "lock"]


def test_checkout_refuses_a_deleted_customer_under_the_lock(customer_client: APIClient, customer: Customer) -> None:
    fill(customer, (product(), 1))
    real_lock = services.lock_customer

    def lock_then_deleted(customer_id: int) -> Any:
        Customer.objects.filter(pk=customer_id).update(status="deleted", deleted_at=real_now())
        return real_lock(customer_id)

    with mock.patch.object(services, "lock_customer", lock_then_deleted):
        response = checkout(customer_client, pickup_body(100_000))
    assert response.status_code == 401 and response.json()["code"] == "SESSION_EXPIRED"
    assert not Order.objects.exists()
