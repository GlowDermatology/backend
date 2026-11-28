"""What the website shows from ``appointments`` (``core.storefront``): a booking, a cancellation or a
reschedule changes the specialists' next free slots."""

from __future__ import annotations

from glowderma.appointments.models import Appointment
from glowderma.core import storefront


def connect() -> None:
    storefront.watch(Appointment, [storefront.AVAILABILITY])


__all__ = [
    "connect",
]
