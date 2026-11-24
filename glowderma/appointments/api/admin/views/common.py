"""What the admin ``appointments`` views share: the rendered item and the appointment lookup."""

from __future__ import annotations

from typing import Any

from rest_framework.exceptions import NotFound

from glowderma.appointments import selectors
from glowderma.appointments.api.admin.serializers import (
    AppointmentItemSerializer,
)
from glowderma.appointments.models import Appointment


def appointment_item(appointment: Appointment) -> dict[str, Any]:
    fresh = selectors.get_appointment(appointment.pk)
    return dict(AppointmentItemSerializer(fresh).data)


def appointment_or_404(pk: int) -> Appointment:
    appointment = Appointment.objects.filter(pk=pk).first()
    if appointment is None:
        raise NotFound
    return appointment


__all__ = [
    "appointment_item",
    "appointment_or_404",
]
