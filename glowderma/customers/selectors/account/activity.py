"""The customer's last activity and the dormant-account rule it feeds (customer contract §2.1.5)."""

from __future__ import annotations

from datetime import datetime, timedelta

from django.conf import settings
from django.db.models import Max

from glowderma.accounts.models import AuthSession, Customer
from glowderma.appointments import selectors as appointments_selectors
from glowderma.commerce.models import Order
from glowderma.loyalty.models import Purchase


def last_activity_at(customer_id: int) -> datetime | None:
    """The dormant-account rule's last activity (customer contract §2.2.3): the newest of
    ``Customer.last_login_at``, the newest customer session ``last_used_at``, the newest ``Purchase``, the
    newest order and the appointment activity of ``appointments.selectors.last_activity_at`` (every
    appointment's ``created_at``, staff bookings included, and the ``start`` of open or attended ones)."""
    candidates: list[datetime | None] = [
        Customer.objects.filter(pk=customer_id).values_list("last_login_at", flat=True).first(),
        AuthSession.objects.filter(customer_id=customer_id, audience="customer").aggregate(
            newest=Max("last_used_at")
        )["newest"],
        Purchase.objects.filter(customer_id=customer_id).aggregate(newest=Max("at"))["newest"],
        Order.objects.filter(customer_id=customer_id).aggregate(newest=Max("created_at"))["newest"],
        appointments_selectors.last_activity_at(customer_id),
    ]
    values = [value for value in candidates if value is not None]
    return max(values) if values else None


def is_dormant(customer_id: int, *, now: datetime) -> tuple[bool, datetime | None]:
    """``(dormant, last_activity_at)``: inactive for longer than ``CUSTOMER_DORMANT_DAYS`` (no activity at all
    counts as dormant)."""
    last = last_activity_at(customer_id)
    threshold = now - timedelta(days=int(settings.CUSTOMER_DORMANT_DAYS))
    return (last is None or last < threshold), last


__all__ = [
    "is_dormant",
    "last_activity_at",
]
