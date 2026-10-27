"""factory-boy factories for the payments models (call them as ``XFactory.create(...)``).

``PaymentFactory`` is a **paid** Click payment of a processing order by default (``provider_txn_id`` in the
contract's ``CLK-…`` style, ``confirmed_at`` three minutes after creation); the traits ``pending`` (an
awaiting-payment order, no transaction yet), ``failed`` (provider error ``-9``) and ``refunded`` (fully
returned) keep the ``payment_status_times`` / ``payment_paid_has_txn`` CHECKs satisfied. ``RefundFactory``
is a ``requested`` full refund of a paid payment; one trait per further state (``submitting``,
``succeeded``, ``failed``, ``dead_letter``, ``manual_required``, ``unknown``). B5 builds its services and the
fake Click/Payme adapters on top of these rows. ``OrderRowFactory`` writes the ``commerce.Order`` the rows
point at.
"""

from datetime import datetime, timedelta
from typing import Any

from factory.declarations import LazyAttribute, LazyAttributeSequence, LazyFunction, SubFactory, Trait
from factory.django import DjangoModelFactory

from glowderma.accounts.tests.factories import CustomerFactory, StaffUserFactory
from glowderma.core import enums, timeutils
from glowderma.payments.models import Payment, Refund


def _now() -> datetime:
    return timeutils.now()


class OrderRowFactory(DjangoModelFactory[Any]):
    """A ``commerce.Order`` row for tests of this lower module.

    ``payments`` sits below ``commerce`` (import-linter layers), so it may not import
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


def _txn_id(payment: Payment, n: int) -> str:
    """Contract-style ids (P-3): ``CLK-7741920385`` for Click, a 24-hex string for Payme."""
    if payment.provider == enums.PaymentProvider.PAYME:
        return f"{0x68FA0C3E0000000000000000 + n:024x}"
    return f"CLK-{7741920385 + n}"


class PaymentFactory(DjangoModelFactory[Payment]):
    class Meta:
        model = Payment

    class Params:
        pending = Trait(  # type: ignore[no-untyped-call]
            order=SubFactory(OrderRowFactory),  # type: ignore[no-untyped-call]
            status=enums.PaymentStatus.PENDING,
            provider_txn_id="",
            confirmed_at=None,
        )
        failed = Trait(  # type: ignore[no-untyped-call]
            order=SubFactory(OrderRowFactory),  # type: ignore[no-untyped-call]
            status=enums.PaymentStatus.FAILED,
            confirmed_at=None,
            failed_at=LazyAttribute(lambda o: o.created_at + timedelta(minutes=2)),  # type: ignore[no-untyped-call]
            error_code="-9",
            error_message="Transaction cancelled",
        )
        refunded = Trait(  # type: ignore[no-untyped-call]
            order=SubFactory(OrderRowFactory, cancelled=True),  # type: ignore[no-untyped-call]
            status=enums.PaymentStatus.REFUNDED,
            refunded_at=LazyAttribute(lambda o: (o.confirmed_at or o.created_at) + timedelta(hours=1)),  # type: ignore[no-untyped-call]
        )

    order = SubFactory(OrderRowFactory, paid=True)  # type: ignore[no-untyped-call]
    provider = LazyAttribute(lambda o: o.order.payment_provider)  # type: ignore[no-untyped-call]
    status = enums.PaymentStatus.PAID
    amount = LazyAttribute(lambda o: o.order.total)  # type: ignore[no-untyped-call]
    provider_txn_id = LazyAttributeSequence(_txn_id)  # type: ignore[no-untyped-call]
    created_at = LazyAttribute(lambda o: o.order.created_at)  # type: ignore[no-untyped-call]
    confirmed_at = LazyAttribute(lambda o: o.created_at + timedelta(minutes=3))  # type: ignore[no-untyped-call]


def _requested_at(refund: Refund) -> datetime:
    payment = refund.payment
    return (payment.confirmed_at or payment.created_at) + timedelta(hours=1)


class RefundFactory(DjangoModelFactory[Refund]):
    class Meta:
        model = Refund

    class Params:
        submitting = Trait(  # type: ignore[no-untyped-call]
            state=enums.RefundState.SUBMITTING,
            submitted_at=LazyAttribute(lambda o: o.requested_at + timedelta(seconds=10)),  # type: ignore[no-untyped-call]
            last_attempt_at=LazyAttribute(lambda o: o.submitted_at),  # type: ignore[no-untyped-call]
            attempts=1,
        )
        succeeded = Trait(  # type: ignore[no-untyped-call]
            state=enums.RefundState.SUCCEEDED,
            submitted_at=LazyAttribute(lambda o: o.requested_at + timedelta(seconds=10)),  # type: ignore[no-untyped-call]
            last_attempt_at=LazyAttribute(lambda o: o.submitted_at),  # type: ignore[no-untyped-call]
            completed_at=LazyAttribute(lambda o: o.submitted_at + timedelta(seconds=5)),  # type: ignore[no-untyped-call]
            attempts=1,
            provider_refund_id=LazyAttribute(lambda o: f"RF-{o.payment.provider_txn_id}"),  # type: ignore[no-untyped-call]
        )
        failed = Trait(  # type: ignore[no-untyped-call]
            state=enums.RefundState.FAILED,
            submitted_at=LazyAttribute(lambda o: o.requested_at + timedelta(seconds=10)),  # type: ignore[no-untyped-call]
            last_attempt_at=LazyAttribute(lambda o: o.submitted_at),  # type: ignore[no-untyped-call]
            attempts=1,
            error_code="-31050",
            last_error="Provider refused the reversal",
        )
        dead_letter = Trait(  # type: ignore[no-untyped-call]
            state=enums.RefundState.FAILED,
            error_code="dead_letter",
            last_error="refund.submit: 10 transport failures",
        )
        manual_required = Trait(  # type: ignore[no-untyped-call]
            payment=SubFactory(PaymentFactory, order__payme=True),  # type: ignore[no-untyped-call]
            state=enums.RefundState.MANUAL_REQUIRED,
            manual_reason=enums.RefundManualReason.PAYME,
        )
        unknown = Trait(  # type: ignore[no-untyped-call]
            state=enums.RefundState.UNKNOWN,
            submitted_at=LazyAttribute(lambda o: o.requested_at + timedelta(seconds=10)),  # type: ignore[no-untyped-call]
            last_attempt_at=LazyAttribute(lambda o: o.submitted_at),  # type: ignore[no-untyped-call]
            attempts=1,
            last_error="status check timed out after submission",
        )

    payment = SubFactory(PaymentFactory)  # type: ignore[no-untyped-call]
    state = enums.RefundState.REQUESTED
    amount = LazyAttribute(lambda o: o.payment.amount)  # type: ignore[no-untyped-call]
    origin = Refund.Origin.ORDER_CANCELLED
    requested_at = LazyAttribute(_requested_at)  # type: ignore[no-untyped-call]
    created_at = LazyAttribute(lambda o: o.requested_at)  # type: ignore[no-untyped-call]


__all__ = ["OrderRowFactory", "PaymentFactory", "RefundFactory"]
