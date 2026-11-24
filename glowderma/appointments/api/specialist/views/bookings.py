"""The specialist's actions on an own appointment: confirm or complete, reschedule and cancel."""

from __future__ import annotations

from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.appointments import services
from glowderma.appointments.api.admin.serializers import (
    AppointmentCancelSerializer,
)
from glowderma.appointments.api.specialist.base import OwnedAppointmentAPIView, appointment_item
from glowderma.appointments.api.specialist.schemas.bookings import (
    SPECIALIST_APPOINTMENT_ACTION_POST,
    SPECIALIST_APPOINTMENT_CANCEL_POST,
    SPECIALIST_APPOINTMENT_RESCHEDULE_POST,
)
from glowderma.appointments.api.specialist.serializers import (
    SpecialistRescheduleSerializer,
)
from glowderma.core import enums
from glowderma.core.idempotency import idempotent


class SpecialistAppointmentActionView(OwnedAppointmentAPIView):
    @SPECIALIST_APPOINTMENT_ACTION_POST
    @idempotent()
    def post(self, request: Request, pk: int, action: str) -> Response:
        if action not in enums.AppointmentAction.values:
            raise NotFound
        services.transition(
            pk, action, actor=request.user, request=request, specialist_account_id=self.account_id
        )
        return Response(appointment_item(pk))


class SpecialistAppointmentRescheduleView(OwnedAppointmentAPIView):
    @SPECIALIST_APPOINTMENT_RESCHEDULE_POST
    @idempotent()
    def post(self, request: Request, pk: int) -> Response:
        body = SpecialistRescheduleSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        services.reschedule(
            pk,
            **body.validated_data,
            actor=request.user,
            request=request,
            specialist_account_id=self.account_id,
        )
        return Response(appointment_item(pk))


class SpecialistAppointmentCancelView(OwnedAppointmentAPIView):
    @SPECIALIST_APPOINTMENT_CANCEL_POST
    @idempotent()
    def post(self, request: Request, pk: int) -> Response:
        body = AppointmentCancelSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        services.cancel(
            pk,
            reason=body.validated_data["reason"],
            actor=request.user,
            request=request,
            specialist_account_id=self.account_id,
        )
        return Response(appointment_item(pk))


__all__ = [
    "SpecialistAppointmentActionView",
    "SpecialistAppointmentCancelView",
    "SpecialistAppointmentRescheduleView",
]
