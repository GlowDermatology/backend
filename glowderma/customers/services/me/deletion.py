"""Account deletion (customer contract §2.2.3): cancel what is still open, keep what is settled."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from django.db import transaction

from glowderma.accounts import services as account_services
from glowderma.accounts.models import Customer
from glowderma.appointments import services as appointment_services
from glowderma.appointments.models import Appointment
from glowderma.commerce import selectors as commerce_selectors
from glowderma.commerce import services as commerce_services
from glowderma.commerce.models import Order
from glowderma.core import enums, timeutils
from glowderma.core.api.exceptions import (
    CustomerError,
    PaymentInProgress,
)
from glowderma.customers.services.common import trail
from glowderma.customers.services.me import reauth
from glowderma.delivery import services as delivery_services


@dataclass(frozen=True, slots=True)
class DeletionResult:
    orders_cancelled: int
    appointments_cancelled: int


OPEN_APPOINTMENT_STATUSES = (enums.AppointmentStatus.REQUESTED, enums.AppointmentStatus.CONFIRMED)


def _cancel_unless_settled(cancel: Callable[..., object], *args: Any, **kwargs: Any) -> bool:
    """Run one ``DELETE me/`` cancel in a savepoint; ``False`` when it answered 409 ``not_cancellable``."""
    try:
        with transaction.atomic():
            cancel(*args, **kwargs)
    except CustomerError as exc:
        if exc.get_codes() != "not_cancellable":
            raise
        return False
    return True


def delete_account(
    customer_id: int,
    *,
    password: str | None = None,
    verification_token: str | None = None,
    sid: str | account_services.NoSession,
    request: Any = None,
) -> DeletionResult:
    """``DELETE me/`` steps 1–6: re-authentication (``customer_reauth``), then in one transaction under the
    customer-level lock (``FOR NO KEY UPDATE``, before any order lock): 409 ``payment_in_progress`` while a
    Click payment is in flight (nothing written), cancel unpaid orders and open future appointments, delete
    the cart lines, ``accounts.services.delete_customer`` and audit ``customer.deleted``."""
    snapshot = Customer.objects.get(pk=customer_id)
    needed = reauth.reauthenticate(
        snapshot, password=password, verification_token=verification_token, sid=sid
    )
    with transaction.atomic():
        customer = account_services.lock_signed_in_customer(customer_id, sid)
        inflight = commerce_selectors.customer_inflight_order(customer_id)
        if inflight is not None:
            raise PaymentInProgress(
                details={"provider": enums.PaymentProvider.CLICK.value, "order_id": inflight},
                params={"provider": "Click"},
            )
        customer = reauth.lock_reauthenticated(customer_id, sid, snapshot, verification_token, needed)
        now = timeutils.now()
        order_ids = list(
            Order.objects.filter(customer_id=customer_id, status=enums.OrderStatus.AWAITING_PAYMENT)
            .order_by("pk")
            .values_list("pk", flat=True)
        )
        # An order or appointment selected here can stop being cancellable before its own row lock is taken
        # (a Click capture committed, staff settled the visit, it started): it is skipped and not counted,
        # never a reason to abort the delete (C§2.2.3 steps 2–3). Each cancel runs in a savepoint.
        orders_cancelled = sum(
            _cancel_unless_settled(commerce_services.cancel_by_customer, order_id, customer_id, at=now)
            for order_id in order_ids
        )
        appointment_ids = list(
            Appointment.objects.filter(
                customer_id=customer_id, status__in=OPEN_APPOINTMENT_STATUSES, start__gt=now
            )
            .order_by("pk")
            .values_list("pk", flat=True)
        )
        appointments_cancelled = sum(
            _cancel_unless_settled(
                appointment_services.cancel_by_customer,
                appointment_id,
                customer_id,
                reason="account_deleted",
                at=now,
                account_deleted=True,
            )
            for appointment_id in appointment_ids
        )
        commerce_services.delete_cart_lines(customer_id)
        # unused delivery quotes copy the address text and pin: they go with the saved addresses
        delivery_services.delete_unused_quotes(customer_id)
        account_services.delete_customer(customer_id)
        result = DeletionResult(orders_cancelled, appointments_cancelled)
        trail.audit_customer(
            "customer.deleted",
            customer,
            request,
            {
                "orders_cancelled": result.orders_cancelled,
                "appointments_cancelled": result.appointments_cancelled,
            },
        )
        return result


__all__ = [
    "OPEN_APPOINTMENT_STATUSES",
    "DeletionResult",
    "delete_account",
]
