"""Helpers of the commerce tests: products with stock, orders placed and paid through the services."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from glowderma.accounts.models import Customer, StaffUser
from glowderma.accounts.tests.factories import CustomerFactory, DeviceFactory, StaffUserFactory
from glowderma.catalog.models import Product
from glowderma.catalog.tests.factories import ProductFactory, ProductImageFactory, ProductStockFactory
from glowderma.commerce import services, tasks
from glowderma.commerce.models import Order
from glowderma.core import enums
from glowderma.core.models import OutboxMessage
from glowderma.core.seeding import pinned_clock
from glowderma.core.timeutils import TASHKENT
from glowderma.integrations.bts.registry import get_bts
from glowderma.integrations.click import fake as fake_click
from glowderma.integrations.payme import fake as fake_payme
from glowderma.loyalty import services as loyalty_services
from glowderma.loyalty.tests.support import seed_policies
from glowderma.payments import selectors as payment_selectors

AT = datetime(2026, 10, 20, 12, 0, tzinfo=TASHKENT)
ADDRESS = "Toshkent, Chilonzor tumani, 9-kvartal, 12-uy, 45-xonadon"

__all__ = [
    "ADDRESS",
    "AT",
    "admin",
    "deliver",
    "pay",
    "place",
    "product",
    "seed_policies",
    "ship",
    "shopper",
]


def at(day: int, hour: int = 12, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, second, tzinfo=TASHKENT)


def admin() -> StaffUser:
    return StaffUserFactory.create(full_name="Administrator")


def shopper(*, device: bool = True, **kwargs: Any) -> Customer:
    customer = CustomerFactory.create(**kwargs)
    if device:
        DeviceFactory.create(customer=customer)
    return customer


def product(
    price: int = 100_000, *, promo: bool = False, stock: int = 10, image: bool = False, **kw: Any
) -> Product:
    item = ProductFactory.create(price=price, promo=promo, **kw)
    ProductStockFactory.create(product=item, quantity=stock)
    if image:
        ProductImageFactory.create(product=item)
    return item


def place(
    customer: Customer | None = None,
    lines: Sequence[tuple[Product, int]] | None = None,
    *,
    fulfilment: str = enums.FulfilmentMethod.DELIVERY,
    provider: str = enums.PaymentProvider.CLICK,
    cashback: int = 0,
    when: datetime = AT,
    fake: dict[str, Any] | None = None,
    region_code: str = "10",
    **kwargs: Any,
) -> Order:
    """An ``awaiting_payment`` order placed through ``create_order`` at ``when``."""
    customer = customer or shopper()
    lines = lines if lines is not None else [(product(), 1)]
    delivery = fulfilment == enums.FulfilmentMethod.DELIVERY
    if "delivery_fee" not in kwargs:
        bts = (fake or {}).get("bts")
        kwargs["delivery_fee"] = get_bts(bts).quote(region_code, "1001") if delivery else 0
    with pinned_clock(when):
        order_id = services.create_order(
            customer.pk,
            [(item.pk, quantity) for item, quantity in lines],
            fulfilment,
            payment_provider=provider,
            channel=enums.OrderChannel.IOS,
            address_text=ADDRESS if delivery else "",
            region_code=region_code if delivery else "",
            city_code="1001" if delivery else "",
            cashback_used=cashback,
            metadata={"fake": fake} if fake else None,
            **kwargs,
        )
    return Order.objects.get(pk=order_id)


def txn(order: Order) -> str:
    if order.payment_provider == enums.PaymentProvider.CLICK:
        return fake_click.transaction_id(order.pk)
    return fake_payme.transaction_id(order.pk)


def pending_payment_id(order: Order) -> int:
    pending = payment_selectors.active_payment(order.pk, enums.PaymentStatus.PENDING)
    assert pending is not None
    return pending.id


def pay(order: Order, when: datetime | None = None, *, paydoc: bool = True) -> Order:
    """``mark_paid`` three minutes after checkout (``processing``); a Click payment gets a ``click_paydoc_id``
    first (as its Prepare would store it) unless ``paydoc=False``."""
    payment_id = pending_payment_id(order)
    raw = None
    if paydoc and order.payment_provider == enums.PaymentProvider.CLICK:
        raw = {"click_paydoc_id": fake_click.paydoc_id(order.pk)}
    with pinned_clock(when or order.created_at + timedelta(minutes=3)):
        services.mark_paid(order.pk, payment_id, order.payment_provider, txn(order), raw=raw)
    order.refresh_from_db()
    return order


def deliver(topic: str, order_id: int) -> int:
    """Run the commerce handler of every pending ``topic`` message of the order (as the dispatcher would)."""
    handlers = {
        "bts.create_shipment": tasks.handle_bts_create_shipment,
        "billz.push_sale": tasks.handle_billz_push_sale,
        "refund.submit": tasks.handle_refund_submit,
    }
    count = 0
    for message in OutboxMessage.objects.filter(
        topic=topic, status="pending", payload__order_id=order_id
    ).order_by("id"):
        handlers[topic](message)
        OutboxMessage.objects.filter(pk=message.pk).update(status="done")
        count += 1
    return count


def ship(order: Order, when: datetime | None = None) -> Order:
    """Pay a delivery order and let the BTS create handler run (shipment ``created``, status 100)."""
    if order.status == enums.OrderStatus.AWAITING_PAYMENT:
        pay(order)
    with pinned_clock(when or order.created_at + timedelta(minutes=4)):
        deliver("bts.create_shipment", order.pk)
    order.refresh_from_db()
    return order


def credit(customer: Customer, amount: int, when: datetime) -> None:
    """Give the customer spendable cashback through a store sale (1 % under the seeded policies)."""
    with pinned_clock(when):
        loyalty_services.record_store_purchase(
            customer.pk, f"S-T{customer.pk}-{amount}", "", amount * 100, when, at=when
        )
