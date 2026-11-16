"""Helpers of the Yandex quote / checkout / customer-order tests (plan §3.4): a configured warehouse open
every day, saved addresses with and without a pin, a frozen clock and a counter of the fake's
``check-price``."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime, time
from decimal import Decimal
from typing import Any

import pytest
from django.test import override_settings
from rest_framework.test import APIClient

from glowderma.accounts import services as account_services
from glowderma.accounts.models import Address, Customer
from glowderma.commerce.models import Cart, CartItem
from glowderma.core import timeutils
from glowderma.core.seeding import pinned_clock
from glowderma.delivery import pricing
from glowderma.delivery import selectors as delivery_selectors
from glowderma.delivery.models import DeliverySettings, WarehouseHours
from glowderma.delivery.tests.factories import TASHKENT_PIN
from glowderma.integrations.yandex_delivery import fake as fake_yandex
from glowderma.integrations.yandex_delivery.ports import Point

CALCULATE = "/api/v1/delivery/calculate/"
CHECKOUT = "/api/v1/orders/checkout/"
SCENARIO = {"FAKE_SCENARIO_HEADER_ENABLED": True}
WAREHOUSE_PIN = (Decimal("41.299496"), Decimal("69.240073"))
WAREHOUSE_ADDRESS = "Toshkent, Amir Temur ko‘chasi, 1"
CUSTOMER_PIN = TASHKENT_PIN
ADDRESS_TEXT = "Toshkent, Chilonzor tumani, 9-kvartal, 12-uy"
LEGACY_KEYS = {
    "fulfilment",
    "delivery_fee",
    "region_code",
    "city_code",
    "weight_g",
    "pickup_address",
    "quoted_at",
    "items_subtotal",
    "cashback_max",
    "cashback_to_use",
    "expected_total",
}
NEW_KEYS = {"provider", "method", "quote_id", "quote_expires_at", "options"}
OPTION_KEYS = {
    "provider",
    "method",
    "label",
    "available",
    "unavailable_reason",
    "fee",
    "eta_from",
    "eta_to",
    "scheduled",
    "quote_id",
    "expires_at",
    "expected_total",
}


@pytest.fixture(autouse=True)
def fresh_fake_yandex() -> Iterator[None]:
    fake_yandex.reset()
    yield
    fake_yandex.reset()


@pytest.fixture
def frozen() -> Iterator[datetime]:
    """``timeutils.now`` pinned to the real current second for the whole test (ETA windows are exact)."""
    instant = timeutils.now().replace(microsecond=0)
    with pinned_clock(instant):
        yield instant


@pytest.fixture
def price_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every ``check_price`` the fake answers (its taxi class), in order."""
    calls: list[str] = []
    original = fake_yandex.FakeYandexDelivery.check_price

    def counting(self: Any, source: Any, destination: Any, item: Any, taxi_class: str, **kwargs: Any) -> Any:
        calls.append(taxi_class)
        return original(self, source, destination, item, taxi_class, **kwargs)

    monkeypatch.setattr(fake_yandex.FakeYandexDelivery, "check_price", counting)
    return calls


def configure_warehouse(*, standard: bool = True, express: bool = True, **fields: Any) -> DeliverySettings:
    """Point A complete, both methods on (unless switched off), open 00:00–23:59 every day, no dispatch
    cutoff."""
    values: dict[str, Any] = {
        "warehouse_name": "GlowDerma",
        "warehouse_address": WAREHOUSE_ADDRESS,
        "warehouse_latitude": WAREHOUSE_PIN[0],
        "warehouse_longitude": WAREHOUSE_PIN[1],
        "contact_name": "Ombor",
        "contact_phone": "+998901234567",
        "contact_email": "ombor@glowderma.uz",
        "standard_enabled": standard,
        "express_enabled": express,
        "prep_minutes": 30,
        "dispatch_cutoff_minutes": 0,
        **fields,
    }
    DeliverySettings.objects.filter(pk=1).update(**values)
    for weekday in range(7):
        WarehouseHours.objects.update_or_create(
            weekday=weekday, defaults={"opens_at": time(0), "closes_at": time(23, 59)}
        )
    return DeliverySettings.objects.get(pk=1)


def pinned_address(
    customer: Customer, *, pin: bool = True, region: str = "10", city: str = "1001", text: str = ADDRESS_TEXT
) -> Address:
    return account_services.add_address(
        customer.pk,
        text=text,
        region_code=region,
        city_code=city,
        latitude=CUSTOMER_PIN[0] if pin else None,
        longitude=CUSTOMER_PIN[1] if pin else None,
    )


def fill(customer: Customer, *lines: tuple[Any, int]) -> None:
    cart, _ = Cart.objects.get_or_create(customer=customer)
    for item, quantity in lines:
        CartItem.objects.create(cart=cart, product=item, quantity=quantity, price_at_add=item.price)


def key() -> dict[str, Any]:
    return {"HTTP_IDEMPOTENCY_KEY": str(uuid.uuid4())}


def scenario_header(yandex: dict[str, Any] | None = None, **other: Any) -> dict[str, str]:
    import json

    value = {**({"yandex": yandex} if yandex is not None else {}), **other}
    return {"HTTP_X_GD_FAKE_SCENARIO": json.dumps(value)}


def calculate(
    client: APIClient, address: Address | None = None, *, header: dict[str, str] | None = None, **body: Any
) -> Any:
    payload: dict[str, Any] = {"fulfilment": "delivery", "with_options": True, **body}
    if address is not None:
        payload.setdefault("address_id", address.pk)
    with override_settings(**SCENARIO):
        extra: dict[str, Any] = dict(header or {})
        return client.post(CALCULATE, payload, format="json", **extra)


def checkout(client: APIClient, body: dict[str, Any], *, header: dict[str, str] | None = None) -> Any:
    with override_settings(**SCENARIO):
        return client.post(CHECKOUT, body, format="json", **{**key(), **(header or {})})


def yandex_body(address: Address, quote: dict[str, Any], **over: Any) -> dict[str, Any]:
    return {
        "fulfilment": "delivery",
        "address_id": address.pk,
        "payment_provider": "click",
        "expected_total": quote["expected_total"],
        "delivery_quote_id": quote["quote_id"],
        **over,
    }


def fake_estimate(taxi_class: str, *, pin: tuple[Decimal, Decimal] = CUSTOMER_PIN) -> int:
    """The fake's default estimate from the warehouse to ``pin``."""
    source = Point(lon=float(WAREHOUSE_PIN[1]), lat=float(WAREHOUSE_PIN[0]), fullname=WAREHOUSE_ADDRESS)
    destination = Point(lon=float(pin[1]), lat=float(pin[0]), fullname=ADDRESS_TEXT)
    return fake_yandex.price_for(source, destination, taxi_class)


def expected_fee(taxi_class: str, *, price: int | None = None, **pin: Any) -> int:
    row = DeliverySettings.objects.get(pk=1)
    estimate = price if price is not None else fake_estimate(taxi_class, **pin)
    return pricing.customer_fee(
        estimate, markup_percent=row.markup_percent, vat_percent=row.estimate_vat_percent
    )


def expected_window(method: str, now: datetime) -> Any:
    settings_row = DeliverySettings.objects.get(pk=1)
    config = delivery_selectors.method_settings(method, settings_row)
    calendar = delivery_selectors.warehouse_calendar(timeutils.local_date(now))
    return calendar.eta_window(config.eta_min_minutes, config.eta_max_minutes, now)


def option(body: dict[str, Any], provider: str, method: str | None = None) -> dict[str, Any]:
    return next(o for o in body["options"] if o["provider"] == provider and o["method"] == method)
