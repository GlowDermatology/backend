"""The specialist's own schedule conflicts."""

from __future__ import annotations

from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.appointments import selectors
from glowderma.appointments.api.specialist.base import item_context
from glowderma.appointments.api.specialist.schemas.conflicts import CONFLICTS_GET
from glowderma.appointments.api.specialist.serializers import SpecialistConflictsSerializer
from glowderma.professionals.api.specialist import SpecialistProfileAPIView


class ConflictsView(SpecialistProfileAPIView):
    @CONFLICTS_GET
    def get(self, request: Request) -> Response:
        conflicts = selectors.schedule_conflicts(self.professional)
        return Response(SpecialistConflictsSerializer({"results": conflicts}, context=item_context()).data)


__all__ = [
    "ConflictsView",
]
