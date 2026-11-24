"""The admin boards (contract §2.9): the day board with its bookings, the range boards, the pending summary
and the booking form's meta."""

from __future__ import annotations

from typing import Any, cast

from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.base import AdminAPIView
from glowderma.appointments import selectors, services
from glowderma.appointments.api.admin.schemas.board import (
    APPOINTMENT_RANGE_GET,
    APPOINTMENTS_GET,
    APPOINTMENTS_META_GET,
    APPOINTMENTS_POST,
    PENDING_SUMMARY_GET,
)
from glowderma.appointments.api.admin.serializers import (
    AppointmentCreateSerializer,
    AppointmentRangeSerializer,
    AppointmentsMetaSerializer,
    DayQuerySerializer,
    DayScheduleSerializer,
    PendingSummarySerializer,
    RangeQuerySerializer,
)
from glowderma.appointments.api.admin.views.common import appointment_item
from glowderma.core import enums, timeutils
from glowderma.core.idempotency import idempotent
from glowderma.professionals import selectors as professionals_selectors


class AppointmentsView(AdminAPIView):
    @APPOINTMENTS_GET
    def get(self, request: Request) -> Response:
        query = DayQuerySerializer(data=request.query_params.dict())
        query.is_valid(raise_exception=True)
        day = query.validated_data.get("date") or timeutils.local_date(timeutils.now())
        return Response(DayScheduleSerializer(selectors.day_view(day)).data)

    @APPOINTMENTS_POST
    @idempotent(required=False)
    def post(self, request: Request) -> Response:
        serializer = AppointmentCreateSerializer(data=cast(dict[str, Any], request.data))
        serializer.is_valid(raise_exception=True)
        appointment = services.create_by_admin(
            **serializer.validated_data, actor=request.user, request=request
        )
        return Response(appointment_item(appointment), status=201)


class AppointmentRangeView(AdminAPIView):
    @APPOINTMENT_RANGE_GET
    def get(self, request: Request) -> Response:
        query = RangeQuerySerializer(data=request.query_params.dict())
        query.is_valid(raise_exception=True)
        params = query.validated_data
        view = selectors.range_view(
            params["from"],
            params["to"],
            specialist_id=params.get("specialist_id"),
            full=params["detail"] == enums.AppointmentRangeDetail.FULL,
        )
        return Response(AppointmentRangeSerializer(view).data)


class PendingSummaryView(AdminAPIView):
    @PENDING_SUMMARY_GET
    def get(self, request: Request) -> Response:
        return Response(PendingSummarySerializer(selectors.pending_summary()).data)


class AppointmentsMetaView(AdminAPIView):
    @APPOINTMENTS_META_GET
    def get(self, request: Request) -> Response:
        data = {
            "specialists": list(professionals_selectors.booking_catalogue()),
            "slot_step_min": services.SLOT_STEP_MIN,
        }
        return Response(AppointmentsMetaSerializer(data).data)


__all__ = [
    "AppointmentRangeView",
    "AppointmentsMetaView",
    "AppointmentsView",
    "PendingSummaryView",
]
