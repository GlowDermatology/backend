"""``appointments.storefront``: a booking, a cancellation or a reschedule changes the website's free slots."""

from __future__ import annotations

from typing import Any

import pytest

from glowderma.appointments.models import Appointment
from glowderma.appointments.tests.factories import AppointmentFactory
from glowderma.core import enums, timeutils
from glowderma.core.tests.storefront_support import revalidated

pytestmark = pytest.mark.django_db


def test_a_booking_and_its_cancellation_change_the_free_slots(
    django_capture_on_commit_callbacks: Any,
) -> None:
    capture = django_capture_on_commit_callbacks
    holder: dict[str, Any] = {}

    def book() -> None:
        holder["appointment"] = AppointmentFactory.create()

    assert "shop:availability" in revalidated(capture, book)

    def cancel() -> None:
        appointment = holder["appointment"]
        appointment.status = enums.AppointmentStatus.CANCELLED
        appointment.cancelled_at = timeutils.now()
        appointment.cancelled_by = Appointment.CancelledBy.CUSTOMER
        appointment.save(update_fields=["status", "cancelled_at", "cancelled_by"])

    assert revalidated(capture, cancel) == {"shop:availability"}
