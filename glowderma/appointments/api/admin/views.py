"""Admin views of `appointments` (contract §2.9; T10 admin create / reschedule / cancel)."""

from __future__ import annotations

from typing import Any, cast

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.base import AdminAPIView
from glowderma.appointments import selectors, services
from glowderma.appointments.api.admin.serializers import (
    AppointmentCancelSerializer,
    AppointmentCreateSerializer,
    AppointmentItemSerializer,
    AppointmentRescheduleSerializer,
    AppointmentsMetaSerializer,
    DayQuerySerializer,
    DayScheduleSerializer,
    PendingSummarySerializer,
    ScheduleConflictsSerializer,
)
from glowderma.appointments.models import Appointment
from glowderma.core import enums, timeutils
from glowderma.core.api.schema import ADMIN_ERRORS, IDEMPOTENCY_KEY_HEADER
from glowderma.core.idempotency import idempotent
from glowderma.professionals import selectors as professionals_selectors
from glowderma.professionals.models import Professional

ACTION_PARAM = OpenApiParameter(
    name="action",
    type=OpenApiTypes.STR,
    location=OpenApiParameter.PATH,
    enum=list(enums.AppointmentAction.values),
)


def _item(appointment: Appointment) -> dict[str, Any]:
    fresh = selectors.get_appointment(appointment.pk)
    return dict(AppointmentItemSerializer(fresh).data)


def _appointment_or_404(pk: int) -> Appointment:
    appointment = Appointment.objects.filter(pk=pk).first()
    if appointment is None:
        raise NotFound
    return appointment


class AppointmentsView(AdminAPIView):
    @extend_schema(
        operation_id="admin_appointments_day",
        parameters=[DayQuerySerializer],
        responses={200: DayScheduleSerializer, **ADMIN_ERRORS},
    )
    def get(self, request: Request) -> Response:
        query = DayQuerySerializer(data=request.query_params.dict())
        query.is_valid(raise_exception=True)
        day = query.validated_data.get("date") or timeutils.local_date(timeutils.now())
        return Response(DayScheduleSerializer(selectors.day_view(day)).data)

    @extend_schema(
        operation_id="admin_appointments_create",
        description=(
            "Staff booking (requested_via=admin, created directly as confirmed). The slot must sit on a "
            "5-minute grid in the future, inside the specialist's hours for that day (a date override wins), "
            "outside blocked periods and the recurring break, and must not overlap another open appointment "
            "(a concurrent booking of the same slot answers 409 conflict)."
        ),
        request=AppointmentCreateSerializer,
        responses={201: AppointmentItemSerializer, **ADMIN_ERRORS},
    )
    @idempotent(required=False)
    def post(self, request: Request) -> Response:
        serializer = AppointmentCreateSerializer(data=cast(dict[str, Any], request.data))
        serializer.is_valid(raise_exception=True)
        appointment = services.create_by_admin(
            **serializer.validated_data, actor=request.user, request=request
        )
        return Response(_item(appointment), status=201)


class PendingSummaryView(AdminAPIView):
    @extend_schema(
        operation_id="admin_appointments_pending_summary",
        responses={200: PendingSummarySerializer, **ADMIN_ERRORS},
    )
    def get(self, request: Request) -> Response:
        return Response(PendingSummarySerializer(selectors.pending_summary()).data)


class AppointmentActionView(AdminAPIView):
    @extend_schema(
        operation_id="admin_appointments_action",
        description=(
            "confirm / reject (from requested) or attend (from confirmed); otherwise 409 invalid_transition."
        ),
        parameters=[ACTION_PARAM, IDEMPOTENCY_KEY_HEADER],
        request=None,
        responses={200: AppointmentItemSerializer, **ADMIN_ERRORS},
    )
    @idempotent()
    def post(self, request: Request, pk: int, action: str) -> Response:
        if action not in enums.AppointmentAction.values:
            raise NotFound
        _appointment_or_404(pk)
        appointment = services.transition(pk, action, actor=request.user, request=request)
        return Response(_item(appointment))


class AppointmentRescheduleView(AdminAPIView):
    @extend_schema(
        operation_id="admin_appointments_reschedule",
        description=(
            "Move an open (requested|confirmed) appointment; the status is kept. Same slot rules as the "
            "creation. A new specialist re-snapshots duration and price from that specialist's service link."
        ),
        request=AppointmentRescheduleSerializer,
        responses={200: AppointmentItemSerializer, **ADMIN_ERRORS},
    )
    @idempotent(required=False)
    def post(self, request: Request, pk: int) -> Response:
        _appointment_or_404(pk)
        serializer = AppointmentRescheduleSerializer(data=cast(dict[str, Any], request.data))
        serializer.is_valid(raise_exception=True)
        appointment = services.reschedule(
            pk,
            start=serializer.validated_data["start"],
            professional=serializer.validated_data.get("specialist_id"),
            actor=request.user,
            request=request,
        )
        return Response(_item(appointment))


class AppointmentCancelView(AdminAPIView):
    @extend_schema(
        operation_id="admin_appointments_cancel",
        description=(
            "Cancel an open (requested|confirmed) appointment; the slot is freed and the customer is pushed."
        ),
        request=AppointmentCancelSerializer,
        responses={200: AppointmentItemSerializer, **ADMIN_ERRORS},
    )
    @idempotent(required=False)
    def post(self, request: Request, pk: int) -> Response:
        _appointment_or_404(pk)
        serializer = AppointmentCancelSerializer(data=cast(dict[str, Any], request.data))
        serializer.is_valid(raise_exception=True)
        appointment = services.cancel(
            pk, reason=serializer.validated_data["reason"], actor=request.user, request=request
        )
        return Response(_item(appointment))


class AppointmentsMetaView(AdminAPIView):
    @extend_schema(
        operation_id="admin_appointments_meta",
        description=(
            "What the admin booking dialog offers: active specialists (by position, then name) with "
            "their active services (the duration and price a booking would snapshot) and the slot grid "
            "in minutes."
        ),
        responses={200: AppointmentsMetaSerializer, **ADMIN_ERRORS},
    )
    def get(self, request: Request) -> Response:
        data = {
            "specialists": list(professionals_selectors.booking_catalogue()),
            "slot_step_min": services.SLOT_STEP_MIN,
        }
        return Response(AppointmentsMetaSerializer(data).data)


class SpecialistConflictsView(AdminAPIView):
    """Declared here, not in ``professionals``: it renders appointments (the layers forbid the import)."""

    @extend_schema(
        operation_id="admin_specialists_conflicts",
        description=(
            "The specialist's requested/confirmed appointments starting now or later that its current "
            "schedule no longer covers (a day without hours, outside that day's hours, inside a blocked "
            "period or the recurring break), by start. Schedule changes never move or cancel appointments; "
            "reschedule or cancel these through appointments/{id}/reschedule|cancel/."
        ),
        responses={200: ScheduleConflictsSerializer, **ADMIN_ERRORS},
    )
    def get(self, request: Request, pk: int) -> Response:
        professional = Professional.objects.filter(pk=pk).first()
        if professional is None:
            raise NotFound
        conflicts = selectors.schedule_conflicts(professional)
        return Response(ScheduleConflictsSerializer({"results": conflicts}).data)
