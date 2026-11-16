"""Helpers of the Yandex dispatch / status-sync tests (plan §3.6–§3.7): a warehouse open around the clock, a
paid Yandex order with a pinned address and its checkout quote, the dispatch handler run as the outbox would,
and a counter of the fake's calls. Everything runs against the fake adapter (no network)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import time, timedelta
from typing import Any

import pytest

from glowderma.commerce import services, tasks, topics, yandex_flow
from glowderma.commerce.models import Order
from glowderma.commerce.tests.support import pay, place
from glowderma.core import outbox
from glowderma.core.models import OutboxMessage
from glowderma.delivery.models import CourierClaim, DeliveryQuote, DeliverySettings, Shipment, WarehouseHours
from glowderma.delivery.tests.factories import TASHKENT_PIN, DeliveryQuoteFactory
from glowderma.integrations.yandex_delivery import fake as fake_yandex

WAREHOUSE_PIN = ("41.299496", "69.240073")
PAID_FEE = 22_000
QUOTED_CARRIER_PRICE = 20_000
END_OF_DAY = time(23, 59, 59, 999_999)


@pytest.fixture(autouse=True)
def fresh_fake_yandex() -> Iterator[None]:
    fake_yandex.reset()
    yield
    fake_yandex.reset()


@pytest.fixture(autouse=True)
def no_pauses(monkeypatch: pytest.MonkeyPatch) -> None:
    """The estimate's short pauses (``time.sleep``) are skipped."""
    monkeypatch.setattr(yandex_flow, "_sleep", lambda _seconds: None)


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """How often each fake port method was called (create_claim, accept, claim, claims, cancel, …)."""
    counts: dict[str, int] = {}
    for name in ("create_claim", "accept", "claim", "claims", "cancel", "cancel_terms", "tracking_link"):
        original = getattr(fake_yandex.FakeYandexDelivery, name)

        def counting(
            self: Any, *args: Any, _name: str = name, _original: Any = original, **kwargs: Any
        ) -> Any:
            counts[_name] = counts.get(_name, 0) + 1
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(fake_yandex.FakeYandexDelivery, name, counting)
    return counts


def open_warehouse(**fields: Any) -> DeliverySettings:
    """Point A complete, both methods on, open the whole day every day, no dispatch cutoff."""
    values: dict[str, Any] = {
        "warehouse_name": "GlowDerma",
        "warehouse_address": "Toshkent, Amir Temur ko‘chasi, 1",
        "warehouse_latitude": WAREHOUSE_PIN[0],
        "warehouse_longitude": WAREHOUSE_PIN[1],
        "contact_name": "Ombor",
        "contact_phone": "+998901234567",
        "contact_email": "ombor@glowderma.uz",
        "courier_comment": "2-qavat",
        "standard_enabled": True,
        "express_enabled": True,
        "prep_minutes": 0,
        "dispatch_cutoff_minutes": 0,
        **fields,
    }
    DeliverySettings.objects.filter(pk=1).update(**values)
    for weekday in range(7):
        WarehouseHours.objects.update_or_create(
            weekday=weekday, defaults={"opens_at": time(0), "closes_at": END_OF_DAY}
        )
    return DeliverySettings.objects.get(pk=1)


def yandex_order(
    *,
    fake: dict[str, Any] | None = None,
    method: str = "standard",
    pin: bool = True,
    paid: bool = True,
    quote_price: int = QUOTED_CARRIER_PRICE,
) -> Order:
    """A delivery order whose shipment is Yandex (``method``) with a checkout quote, the address pinned
    (unless ``pin=False``) and paid (stage ``awaiting_dispatch``) unless ``paid=False``."""
    order = place(fake={"yandex": fake} if fake is not None else None, delivery_fee=PAID_FEE)
    quote = DeliveryQuoteFactory.create(
        customer=order.customer, method=method, carrier_price=quote_price, customer_fee=PAID_FEE
    )
    Shipment.objects.filter(order=order).update(provider="yandex", method=method, quote=quote)
    if pin:
        Order.objects.filter(pk=order.pk).update(
            address_latitude=TASHKENT_PIN[0], address_longitude=TASHKENT_PIN[1]
        )
    order.refresh_from_db()
    if paid:
        order = pay(order)
    return order


def quote_of(order: Order) -> DeliveryQuote:
    shipment = Shipment.objects.select_related("quote").get(order=order)
    assert shipment.quote is not None
    return shipment.quote


def dispatch(order: Order, staff_id: int, *, weight_g: int = 500, **kwargs: Any) -> CourierClaim:
    """``request_dispatch`` (what the endpoint calls); returns the new claim."""
    claim_pk = services.request_dispatch(order.pk, staff_id, weight_g=weight_g, **kwargs)
    return CourierClaim.objects.get(pk=claim_pk)


def message_of(claim: CourierClaim) -> OutboxMessage:
    return OutboxMessage.objects.get(dedupe_key=topics.yandex_dispatch_key(str(claim.request_id)))


def run(claim: CourierClaim) -> outbox.RetryLater | None:
    """One delivery of the claim's ``yandex.dispatch`` message (as the dispatcher would); a deferral is
    returned instead of raised."""
    try:
        tasks.dispatch_courier(message_of(claim))
    except outbox.RetryLater as exc:
        return exc
    return None


def shipment_of(order: Order) -> Shipment:
    return Shipment.objects.select_related("claim").get(order=order)


def claim_id_of(claim: CourierClaim) -> str:
    claim.refresh_from_db()
    return claim.claim_id


def poll_until(order: Order, status: str, *, limit: int = 20) -> None:
    """Poll (``bulk_info``) until the order's current claim reaches ``status`` (the fake moves one scripted
    status per read)."""
    for _ in range(limit):
        tasks.poll_yandex()
        if shipment_of(order).claim.status == status:  # type: ignore[union-attr]
            return
    raise AssertionError(f"claim never reached {status}")


def later(minutes: int) -> timedelta:
    return timedelta(minutes=minutes)


__all__ = [
    "PAID_FEE",
    "QUOTED_CARRIER_PRICE",
    "calls",
    "claim_id_of",
    "dispatch",
    "fresh_fake_yandex",
    "later",
    "message_of",
    "no_pauses",
    "open_warehouse",
    "poll_until",
    "quote_of",
    "run",
    "shipment_of",
    "yandex_order",
]
