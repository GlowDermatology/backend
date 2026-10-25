"""factory-boy factories for the delivery models (call them as ``XFactory.create(...)``).

``ShipmentFactory`` is the ``not_created`` row checkout makes for an awaiting-payment delivery order; the
traits ``failed`` (one answered BTS attempt), ``created`` (a parcel of a processing order with the contract's
``48213907``-style tracking number, ``tariff = Order.delivery_fee``, ``via = polling``) and ``cancelled``
(a created parcel BTS cancelled) keep ``shipment_state_fields`` satisfied; ``not_created`` is an explicit
no-op trait so every state can be named. Pass ``create_message=`` (a ``core.OutboxMessage``) to make the row
``pending``; :func:`create_message_for` builds one with the design's dedupe key. ``ShipmentEventFactory`` is
the ``created`` answer of a created shipment by default. ``OrderRowFactory`` writes the ``commerce.Order``
the rows point at.
"""

from datetime import datetime, timedelta

from django.db import models
from factory.declarations import LazyAttribute, LazyFunction, SubFactory, Trait
from factory.django import DjangoModelFactory

from glowderma.accounts.tests.factories import CustomerFactory, StaffUserFactory
from glowderma.core import enums, timeutils
from glowderma.core.models import OutboxMessage
from glowderma.delivery.models import Shipment, ShipmentEvent


def _now() -> datetime:
    return timeutils.now()


class OrderRowFactory(DjangoModelFactory[models.Model]):
    """A ``commerce.Order`` row for tests of this lower module.

    ``delivery`` sits below ``commerce`` (import-linter layers), so it may not import
    ``commerce.tests.factories``; like the FK, the model is referenced by label and resolved through the app
    registry. The columns satisfy every CHECK of the order (total formula, address by fulfilment, cancellation
    and ``paid_at`` by status); the traits ``paid`` (processing), ``cancelled`` (admin cancel of a paid order)
    and ``payme`` cover what payments and shipments need. No order items are written: a payment or shipment
    never reads them.
    """

    class Meta:
        model = "commerce.Order"

    class Params:
        paid = Trait(  # type: ignore[no-untyped-call]
            status=enums.OrderStatus.PROCESSING,
            paid_at=LazyAttribute(lambda o: o.created_at + timedelta(minutes=3)),  # type: ignore[no-untyped-call]
            status_changed_at=LazyAttribute(lambda o: o.paid_at),  # type: ignore[no-untyped-call]
        )
        cancelled = Trait(  # type: ignore[no-untyped-call]
            status=enums.OrderStatus.CANCELLED,
            paid_at=LazyAttribute(lambda o: o.created_at + timedelta(minutes=3)),  # type: ignore[no-untyped-call]
            cancelled_at=LazyAttribute(lambda o: o.created_at + timedelta(minutes=60)),  # type: ignore[no-untyped-call]
            status_changed_at=LazyAttribute(lambda o: o.cancelled_at),  # type: ignore[no-untyped-call]
            cancellation_reason=enums.CancellationReason.ADMIN,
            cancellation_note="Mijoz so‘rovi bilan",
            cancelled_by=SubFactory(StaffUserFactory),  # type: ignore[no-untyped-call]
        )
        payme = Trait(payment_provider=enums.PaymentProvider.PAYME)  # type: ignore[no-untyped-call]

    customer = SubFactory(CustomerFactory)  # type: ignore[no-untyped-call]
    channel = enums.OrderChannel.IOS
    fulfilment = enums.FulfilmentMethod.DELIVERY
    address_text = "Toshkent, Chilonzor tumani, 9-kvartal, 12-uy, 45-xonadon"
    address_region_code = "10"
    address_city_code = "1001"
    recipient_name = LazyAttribute(lambda o: o.customer.name)  # type: ignore[no-untyped-call]
    recipient_phone = LazyAttribute(lambda o: o.customer.phone)  # type: ignore[no-untyped-call]
    items_subtotal = 185_000
    promo_subtotal = 0
    delivery_fee = 25_000
    cashback_used = 0
    total = LazyAttribute(lambda o: o.items_subtotal + o.delivery_fee - o.cashback_used)  # type: ignore[no-untyped-call]
    payment_provider = enums.PaymentProvider.CLICK
    reservation_minutes = 15
    created_at = LazyFunction(_now)  # type: ignore[no-untyped-call]
    status_changed_at = LazyAttribute(lambda o: o.created_at)  # type: ignore[no-untyped-call]
    reservation_expires_at = LazyAttribute(  # type: ignore[no-untyped-call]
        lambda o: o.created_at + timedelta(minutes=o.reservation_minutes)
    )


def _tracking_number(shipment: Shipment) -> str:
    return f"{48_213_907 + shipment.order.pk - 10_001}"


def _attempt_at(shipment: Shipment) -> datetime:
    order = shipment.order
    return (order.paid_at or order.created_at) + timedelta(minutes=1)


class ShipmentFactory(DjangoModelFactory[Shipment]):
    class Meta:
        model = Shipment

    class Params:
        not_created = Trait(state=Shipment.State.NOT_CREATED)  # type: ignore[no-untyped-call]
        failed = Trait(  # type: ignore[no-untyped-call]
            order=SubFactory(OrderRowFactory, paid=True),  # type: ignore[no-untyped-call]
            state=Shipment.State.FAILED,
            attempts=1,
            create_requests=1,
            last_attempt_at=LazyAttribute(_attempt_at),  # type: ignore[no-untyped-call]
            last_error_code="bts_rejected",
            last_error="BTS: region not served",
        )
        created = Trait(  # type: ignore[no-untyped-call]
            order=SubFactory(OrderRowFactory, paid=True),  # type: ignore[no-untyped-call]
            state=Shipment.State.CREATED,
            tracking_number=LazyAttribute(_tracking_number),  # type: ignore[no-untyped-call]
            tariff=LazyAttribute(lambda o: o.order.delivery_fee),  # type: ignore[no-untyped-call]
            bts_order_id=LazyAttribute(lambda o: f"bts-{o.order.pk}"),  # type: ignore[no-untyped-call]
            bts_status="100",
            via=enums.ShipmentVia.POLLING,
            bts_created_at=LazyAttribute(_attempt_at),  # type: ignore[no-untyped-call]
            last_status_at=LazyAttribute(lambda o: o.bts_created_at),  # type: ignore[no-untyped-call]
            attempts=1,
            create_requests=1,
            last_attempt_at=LazyAttribute(lambda o: o.bts_created_at),  # type: ignore[no-untyped-call]
        )
        cancelled = Trait(  # type: ignore[no-untyped-call]
            order=SubFactory(OrderRowFactory, cancelled=True),  # type: ignore[no-untyped-call]
            state=Shipment.State.CANCELLED,
            tracking_number=LazyAttribute(_tracking_number),  # type: ignore[no-untyped-call]
            tariff=LazyAttribute(lambda o: o.order.delivery_fee),  # type: ignore[no-untyped-call]
            bts_order_id=LazyAttribute(lambda o: f"bts-{o.order.pk}"),  # type: ignore[no-untyped-call]
            bts_status="100",
            via=enums.ShipmentVia.POLLING,
            bts_created_at=LazyAttribute(_attempt_at),  # type: ignore[no-untyped-call]
            last_status_at=LazyAttribute(lambda o: o.bts_created_at),  # type: ignore[no-untyped-call]
            cancelled_at=LazyAttribute(lambda o: (o.bts_created_at or o.created_at) + timedelta(minutes=30)),  # type: ignore[no-untyped-call]
            attempts=1,
            create_requests=1,
            last_attempt_at=LazyAttribute(lambda o: o.bts_created_at),  # type: ignore[no-untyped-call]
        )

    order = SubFactory(OrderRowFactory)  # type: ignore[no-untyped-call]
    state = Shipment.State.NOT_CREATED
    created_at = LazyAttribute(lambda o: o.order.created_at)  # type: ignore[no-untyped-call]


def create_message_for(shipment: Shipment, *, status: str = "pending", manual: bool = False) -> OutboxMessage:
    """A ``bts.create_shipment`` outbox message for ``shipment`` (design 03 §5 payload and dedupe key)."""
    request_no = shipment.create_requests + 1
    message = OutboxMessage.objects.create(
        topic="bts.create_shipment",
        payload={"order_id": shipment.order_id, "manual": manual, "request_no": request_no, "actor_id": None},
        dedupe_key=f"bts.create_shipment:{shipment.order_id}:{request_no}",
        status=status,
    )
    shipment.create_requests = request_no
    shipment.create_message = message
    shipment.save(update_fields=["create_requests", "create_message"])
    return message


class ShipmentEventFactory(DjangoModelFactory[ShipmentEvent]):
    class Meta:
        model = ShipmentEvent

    shipment = SubFactory(ShipmentFactory, created=True)  # type: ignore[no-untyped-call]
    kind = ShipmentEvent.Kind.CREATED
    bts_status = LazyAttribute(lambda o: o.shipment.bts_status)  # type: ignore[no-untyped-call]
    via = LazyAttribute(lambda o: o.shipment.via)  # type: ignore[no-untyped-call]
    payload = LazyAttribute(  # type: ignore[no-untyped-call]
        lambda o: {"tracking_number": o.shipment.tracking_number, "status": o.shipment.bts_status or None}
    )
    at = LazyAttribute(lambda o: o.shipment.bts_created_at or o.shipment.created_at)  # type: ignore[no-untyped-call]


__all__ = ["OrderRowFactory", "ShipmentEventFactory", "ShipmentFactory", "create_message_for"]
