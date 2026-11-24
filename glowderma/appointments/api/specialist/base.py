"""What the specialist board views share: the rendered item and the view base of an owned appointment."""

from __future__ import annotations

from typing import Any

from rest_framework.exceptions import NotFound
from rest_framework.request import Request

from glowderma.appointments import selectors
from glowderma.appointments.api.specialist.serializers import SpecialistAppointmentItemSerializer
from glowderma.core import timeutils
from glowderma.professionals.api.specialist import SpecialistProfileAPIView


def item_context() -> dict[str, Any]:
    return {"now": timeutils.now()}  # one clock for every item's ``allowed_actions``


def appointment_item(pk: int) -> dict[str, Any]:
    return dict(
        SpecialistAppointmentItemSerializer(selectors.get_appointment(pk), context=item_context()).data
    )


class OwnedAppointmentAPIView(SpecialistProfileAPIView):
    """Another specialist's appointment (or none) answers 404 before anything else."""

    def initial(self, request: Request, *args: Any, **kwargs: Any) -> None:
        super().initial(request, *args, **kwargs)
        if not self.professional.appointments.filter(pk=kwargs["pk"]).exists():
            raise NotFound


__all__ = [
    "OwnedAppointmentAPIView",
    "appointment_item",
    "item_context",
]
