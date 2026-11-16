"""factory-boy factories for the delivery models (call them as ``XFactory.create(...)``).

``ShipmentFactory`` is the ``not_created`` row checkout makes for an awaiting-payment delivery order; the
traits ``failed`` (one answered BTS attempt), ``created`` (a parcel of a processing order with the contract's
``48213907``-style tracking number, ``tariff = Order.delivery_fee``, ``via = polling``) and ``cancelled``
(a created parcel BTS cancelled) keep ``shipment_state_fields`` satisfied; ``not_created`` is an explicit
no-op trait so every state can be named. Pass ``create_message=`` (a ``core.OutboxMessage``) to make the row
``pending``; :func:`create_message_for` builds one with the design's dedupe key. ``ShipmentEventFactory`` is
the ``created`` answer of a created shipment by default. ``OrderRowFactory`` writes the ``commerce.Order``
the rows point at.

Yandex Delivery: the trait ``yandex`` makes a Yandex Standard shipment (``not_created``, no stage) of an
awaiting-payment order; :func:`yandex_shipment` builds one in any state / stage (with its current claim for
``created`` / ``cancelled``, which ``shipment_state_fields`` requires). ``CourierClaimFactory`` is an open
claim of a paid order's Yandex shipment, ``DeliveryIssueFactory`` an open ``dispatch_failed`` issue,
``DeliveryQuoteFactory`` an unused 15-minute Yandex Standard quote.
"""

import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from factory.declarations import LazyAttribute, LazyFunction, SubFactory, Trait
from factory.django import DjangoModelFactory

from glowderma.accounts.tests.factories import CustomerFactory, StaffUserFactory
from glowderma.core import enums, timeutils
from glowderma.core.models import OutboxMessage
from glowderma.delivery.models import CourierClaim, DeliveryIssue, DeliveryQuote, Shipment, ShipmentEvent

TASHKENT_PIN = (Decimal("41.311081"), Decimal("69.240562"))


def _now() -> datetime:
    return timeutils.now()


class OrderRowFactory(DjangoModelFactory[Any]):
    """A ``commerce.Order`` row for tests of this lower module.

    ``delivery`` sits below ``commerce`` (import-linter layers), so it may not import
    ``commerce.tests.factories``; like the FK, the model is referenced by label and resolved through the app
    registry (hence the ``Any`` type parameter). The columns satisfy every CHECK of the order (total
    formula, address by fulfilment, cancellation and ``paid_at`` by status); the traits ``paid``
    (processing), ``cancelled`` (admin cancel of a paid order)
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
        yandex = Trait(  # type: ignore[no-untyped-call]
            provider=enums.DeliveryProvider.YANDEX,
            method=enums.DeliveryMethod.STANDARD,
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


class CourierClaimFactory(DjangoModelFactory[CourierClaim]):
    """An open claim (status ``new``) of a paid order's Yandex Standard shipment; ``number`` = next one."""

    class Meta:
        model = CourierClaim

    shipment = SubFactory(  # type: ignore[no-untyped-call]
        ShipmentFactory,
        yandex=True,
        order=SubFactory(OrderRowFactory, paid=True),  # type: ignore[no-untyped-call]
    )
    number = LazyAttribute(lambda o: CourierClaim.objects.filter(shipment=o.shipment).count() + 1)  # type: ignore[no-untyped-call]
    request_id = LazyFunction(uuid.uuid4)  # type: ignore[no-untyped-call]
    claim_id = LazyFunction(lambda: uuid.uuid4().hex)  # type: ignore[no-untyped-call]
    status = "new"
    version = 1
    taxi_class = "courier"
    method = LazyAttribute(lambda o: o.shipment.method or enums.DeliveryMethod.STANDARD)  # type: ignore[no-untyped-call]
    quoted_fee = LazyAttribute(lambda o: o.shipment.order.delivery_fee)  # type: ignore[no-untyped-call]


def yandex_shipment(
    *,
    state: str = Shipment.State.NOT_CREATED,
    stage: str = "",
    order: Any = None,
    claim_status: str = "accepted",
    method: str = enums.DeliveryMethod.STANDARD,
    claim_ended: bool = False,
    **fields: Any,
) -> Shipment:
    """A Yandex shipment in ``state`` / ``stage`` of a paid order (or ``order``): ``created`` and
    ``cancelled`` get an accepted current claim with ``claim_status`` (ended when ``claim_ended`` or
    ``cancelled``); the other states get none."""
    order = order or OrderRowFactory.create(paid=True)
    shipment = ShipmentFactory.create(order=order, yandex=True, method=method, stage=stage, **fields)
    if state in (Shipment.State.CREATED, Shipment.State.CANCELLED):
        now = timeutils.now()
        ended = claim_ended or state == Shipment.State.CANCELLED
        claim = CourierClaimFactory.create(
            shipment=shipment,
            status=claim_status,
            accept_sent_at=now,
            accepted_at=now,
            offer_price=20_000,
            ended_at=now if ended else None,
        )
        Shipment.objects.filter(pk=shipment.pk).update(
            state=state,
            claim=claim,
            tariff=claim.offer_price,
            cancelled_at=now if state == Shipment.State.CANCELLED else None,
        )
        shipment.refresh_from_db()
    elif state != Shipment.State.NOT_CREATED:
        Shipment.objects.filter(pk=shipment.pk).update(state=state)
        shipment.refresh_from_db()
    return shipment


class DeliveryIssueFactory(DjangoModelFactory[DeliveryIssue]):
    class Meta:
        model = DeliveryIssue

    order = SubFactory(OrderRowFactory, paid=True)  # type: ignore[no-untyped-call]
    kind = enums.DeliveryIssueKind.DISPATCH_FAILED
    code = "offer_expired"
    urgent = False
    details = LazyAttribute(lambda o: {"order_id": o.order.pk})  # type: ignore[no-untyped-call]


class DeliveryQuoteFactory(DjangoModelFactory[DeliveryQuote]):
    class Meta:
        model = DeliveryQuote

    customer = SubFactory(CustomerFactory)  # type: ignore[no-untyped-call]
    address_text = "Toshkent, Chilonzor tumani, 9-kvartal, 12-uy, 45-xonadon"
    latitude = TASHKENT_PIN[0]
    longitude = TASHKENT_PIN[1]
    provider = enums.DeliveryProvider.YANDEX
    method = enums.DeliveryMethod.STANDARD
    carrier_price = 20_000
    customer_fee = 22_000
    weight_g = 300
    created_at = LazyFunction(_now)  # type: ignore[no-untyped-call]
    eta_from = LazyAttribute(lambda o: o.created_at + timedelta(minutes=90))  # type: ignore[no-untyped-call]
    eta_to = LazyAttribute(lambda o: o.created_at + timedelta(minutes=210))  # type: ignore[no-untyped-call]
    expires_at = LazyAttribute(lambda o: o.created_at + timedelta(minutes=15))  # type: ignore[no-untyped-call]


__all__ = [
    "CourierClaimFactory",
    "DeliveryIssueFactory",
    "DeliveryQuoteFactory",
    "OrderRowFactory",
    "ShipmentEventFactory",
    "ShipmentFactory",
    "create_message_for",
    "yandex_shipment",
]
