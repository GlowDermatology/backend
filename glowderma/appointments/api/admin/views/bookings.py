"""The admin booking actions: confirm or complete, reschedule and cancel."""

from __future__ import annotations

from typing import Any, cast

from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.base import AdminAPIView
from glowderma.appointments import services
from glowderma.appointments.api.admin.schemas.bookings import (
    APPOINTMENT_ACTION_POST,
    APPOINTMENT_CANCEL_POST,
    APPOINTMENT_RESCHEDULE_POST,
)
from glowderma.appointments.api.admin.serializers import (
    AppointmentCancelSerializer,
    AppointmentRescheduleSerializer,
)
from glowderma.appointments.api.admin.views.common import appointment_item, appointment_or_404
from glowderma.core import enums
from glowderma.core.idempotency import idempotent


class AppointmentActionView(AdminAPIView):
    @APPOINTMENT_ACTION_POST
    @idempotent()
    def post(self, request: Request, pk: int, action: str) -> Response:
        if action not in enums.AppointmentAction.values:
            raise NotFound
        appointment_or_404(pk)
        appointment = services.transition(pk, action, actor=request.user, request=request)
        return Response(appointment_item(appointment))


class AppointmentRescheduleView(AdminAPIView):
    @APPOINTMENT_RESCHEDULE_POST
    @idempotent(required=False)
    def post(self, request: Request, pk: int) -> Response:
        appointment_or_404(pk)
        serializer = AppointmentRescheduleSerializer(data=cast(dict[str, Any], request.data))
        serializer.is_valid(raise_exception=True)
        appointment = services.reschedule(
            pk,
            start=serializer.validated_data["start"],
            professional=serializer.validated_data.get("specialist_id"),
            service=serializer.validated_data.get("service_id"),
            location_id=serializer.validated_data["location_id"],
            actor=request.user,
            request=request,
        )
        return Response(appointment_item(appointment))


class AppointmentCancelView(AdminAPIView):
    @APPOINTMENT_CANCEL_POST
    @idempotent(required=False)
    def post(self, request: Request, pk: int) -> Response:
        appointment_or_404(pk)
        serializer = AppointmentCancelSerializer(data=cast(dict[str, Any], request.data))
        serializer.is_valid(raise_exception=True)
        appointment = services.cancel(
            pk, reason=serializer.validated_data["reason"], actor=request.user, request=request
        )
        return Response(appointment_item(appointment))


__all__ = [
    "AppointmentActionView",
    "AppointmentCancelView",
    "AppointmentRescheduleView",
]
