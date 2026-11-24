"""A specialist's schedule conflicts (``specialists/{id}/conflicts/``)."""

from __future__ import annotations

from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.base import AdminAPIView
from glowderma.appointments import selectors
from glowderma.appointments.api.admin.schemas.conflicts import SPECIALIST_CONFLICTS_GET
from glowderma.appointments.api.admin.serializers import (
    ScheduleConflictsSerializer,
)
from glowderma.professionals.models import Professional


class SpecialistConflictsView(AdminAPIView):
    """Declared here, not in ``professionals``: it renders appointments (the layers forbid the import)."""

    @SPECIALIST_CONFLICTS_GET
    def get(self, request: Request, pk: int) -> Response:
        professional = Professional.objects.filter(pk=pk).first()
        if professional is None:
            raise NotFound
        conflicts = selectors.schedule_conflicts(professional)
        return Response(ScheduleConflictsSerializer({"results": conflicts}).data)


__all__ = [
    "SpecialistConflictsView",
]
