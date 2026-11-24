"""The specialist's boards: the day board with its bookings, the range, the meta and the pending summary."""

from __future__ import annotations

from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.appointments import selectors, services
from glowderma.appointments.api.admin.serializers import (
    AppointmentsMetaSerializer,
    DayQuerySerializer,
    PendingSummarySerializer,
)
from glowderma.appointments.api.specialist.base import appointment_item, item_context
from glowderma.appointments.api.specialist.schemas.board import (
    SPECIALIST_APPOINTMENT_RANGE_GET,
    SPECIALIST_APPOINTMENTS_GET,
    SPECIALIST_APPOINTMENTS_META_GET,
    SPECIALIST_APPOINTMENTS_POST,
    SPECIALIST_PENDING_SUMMARY_GET,
)
from glowderma.appointments.api.specialist.serializers import (
    SpecialistAppointmentCreateSerializer,
    SpecialistAppointmentDaySerializer,
    SpecialistAppointmentRangeSerializer,
    SpecialistRangeQuerySerializer,
)
from glowderma.core import enums, timeutils
from glowderma.core.idempotency import idempotent
from glowderma.professionals import selectors as professionals_selectors
from glowderma.professionals.api.specialist import SpecialistProfileAPIView


class SpecialistAppointmentsView(SpecialistProfileAPIView):
    @SPECIALIST_APPOINTMENTS_GET
    def get(self, request: Request) -> Response:
        query = DayQuerySerializer(data=request.query_params.dict())
        query.is_valid(raise_exception=True)
        day = query.validated_data.get("date") or timeutils.local_date(timeutils.now())
        view = selectors.specialist_day_view(self.professional, day)
        return Response(SpecialistAppointmentDaySerializer(view, context=item_context()).data)

    @SPECIALIST_APPOINTMENTS_POST
    @idempotent()
    def post(self, request: Request) -> Response:
        body = SpecialistAppointmentCreateSerializer(
            data=request.data, context={"professional": self.professional}
        )
        body.is_valid(raise_exception=True)
        appointment = services.create_by_specialist(
            professional=self.professional, **body.validated_data, actor=request.user, request=request
        )
        return Response(appointment_item(appointment.pk), status=201)


class SpecialistAppointmentRangeView(SpecialistProfileAPIView):
    @SPECIALIST_APPOINTMENT_RANGE_GET
    def get(self, request: Request) -> Response:
        query = SpecialistRangeQuerySerializer(data=request.query_params.dict())
        query.is_valid(raise_exception=True)
        params = query.validated_data
        view = selectors.range_view(
            params["from"],
            params["to"],
            specialist_id=self.professional.pk,
            full=params["detail"] == enums.AppointmentRangeDetail.FULL,
            always_list=True,
        )
        return Response(SpecialistAppointmentRangeSerializer(view, context=item_context()).data)


class SpecialistAppointmentsMetaView(SpecialistProfileAPIView):
    @SPECIALIST_APPOINTMENTS_META_GET
    def get(self, request: Request) -> Response:
        data = {
            "specialists": list(professionals_selectors.booking_catalogue().filter(pk=self.professional.pk)),
            "slot_step_min": services.SLOT_STEP_MIN,
        }
        return Response(AppointmentsMetaSerializer(data).data)


class SpecialistPendingSummaryView(SpecialistProfileAPIView):
    @SPECIALIST_PENDING_SUMMARY_GET
    def get(self, request: Request) -> Response:
        summary = selectors.pending_summary(professional_id=self.professional.pk)
        return Response(PendingSummarySerializer(summary).data)


__all__ = [
    "SpecialistAppointmentRangeView",
    "SpecialistAppointmentsMetaView",
    "SpecialistAppointmentsView",
    "SpecialistPendingSummaryView",
]
