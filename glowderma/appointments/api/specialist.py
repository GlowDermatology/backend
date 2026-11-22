"""The specialist's appointments board (routes in ``appointments/api/admin/urls.py``).

The admin day, range, meta and pending-summary reads and the admin actions, narrowed to the signed-in
specialist's own profile. The item is the admin ``AppointmentItem`` without the customer's e-mail, plus the
customer's note and the actions the visit allows right now; the staff note is never part of it.
"""

from typing import Any

from drf_spectacular.utils import extend_schema, extend_schema_field
from rest_framework import serializers
from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.appointments import selectors, services
from glowderma.appointments.api.admin.serializers import (
    AppointmentCancelSerializer,
    AppointmentItemSerializer,
    AppointmentRangeSerializer,
    AppointmentsMetaSerializer,
    DayQuerySerializer,
    DayScheduleSerializer,
    PendingSummarySerializer,
    RangeQuerySerializer,
)
from glowderma.appointments.api.admin.views import ACTION_PARAM
from glowderma.appointments.models import Appointment
from glowderma.core import enums, timeutils
from glowderma.core.api.fields import AwareDateTimeField
from glowderma.core.api.schema import ADMIN_ERRORS, IDEMPOTENCY_KEY_HEADER
from glowderma.core.idempotency import idempotent
from glowderma.professionals import selectors as professionals_selectors
from glowderma.professionals.api.specialist import SpecialistProfileAPIView


class SpecialistCustomerRefSerializer(serializers.Serializer[Any]):
    """``CustomerRef`` without the e-mail: a specialist sees who comes and how to call them."""

    id = serializers.IntegerField(source="pk")
    name = serializers.CharField(allow_blank=True)
    phone = serializers.CharField(allow_null=True)


class SpecialistAppointmentItemSerializer(AppointmentItemSerializer):
    """``SpecialistAppointmentItem``: the admin day item with ``customer`` reduced to ``{id, name, phone}``,
    plus ``customer_note`` and ``allowed_actions``."""

    customer = SpecialistCustomerRefSerializer()  # type: ignore[assignment]
    customer_note = serializers.CharField(allow_blank=True)
    allowed_actions = serializers.SerializerMethodField()

    @extend_schema_field(
        serializers.ListField(
            child=serializers.ChoiceField(choices=services.SPECIALIST_ACTIONS),
            help_text=(
                "What the specialist may do now: confirm / reject / reschedule / cancel before the start "
                "(as the status allows), attend from the start on."
            ),
        )
    )
    def get_allowed_actions(self, obj: Appointment) -> list[str]:
        return services.specialist_allowed_actions(obj, self.context.get("now"))


class SpecialistAppointmentDaySerializer(DayScheduleSerializer):
    """``GET specialist/appointments/?date=``: the admin day shape with ``specialists`` = the caller."""

    appointments = SpecialistAppointmentItemSerializer(many=True)


class SpecialistAppointmentRangeSerializer(AppointmentRangeSerializer):
    """``GET specialist/appointments/range/``: the admin range shape of the caller's own board."""

    appointments = SpecialistAppointmentItemSerializer(many=True, allow_null=True)


class SpecialistRangeQuerySerializer(RangeQuerySerializer):
    """The admin range query without ``specialist_id`` (always the caller)."""

    def get_fields(self) -> dict[str, serializers.Field[Any, Any, Any, Any]]:
        fields = super().get_fields()
        del fields["specialist_id"]
        return fields


class SpecialistConflictsSerializer(serializers.Serializer[dict[str, Any]]):
    results = SpecialistAppointmentItemSerializer(many=True)


class SpecialistRescheduleSerializer(serializers.Serializer[dict[str, Any]]):
    start = AwareDateTimeField()
    location_id = serializers.IntegerField(required=False, min_value=1, max_value=2_147_483_647)
    customer_agreed = serializers.BooleanField(required=False, default=False)


def _context() -> dict[str, Any]:
    return {"now": timeutils.now()}  # one clock for every item's ``allowed_actions``


def _item(pk: int) -> dict[str, Any]:
    return dict(SpecialistAppointmentItemSerializer(selectors.get_appointment(pk), context=_context()).data)


class SpecialistAppointmentsView(SpecialistProfileAPIView):
    @extend_schema(
        operation_id="specialist_appointments_day",
        description="The caller's schedule and own appointments of `date` (default today, Tashkent).",
        parameters=[DayQuerySerializer],
        responses={200: SpecialistAppointmentDaySerializer, **ADMIN_ERRORS},
    )
    def get(self, request: Request) -> Response:
        query = DayQuerySerializer(data=request.query_params.dict())
        query.is_valid(raise_exception=True)
        day = query.validated_data.get("date") or timeutils.local_date(timeutils.now())
        view = selectors.specialist_day_view(self.professional, day)
        return Response(SpecialistAppointmentDaySerializer(view, context=_context()).data)


class SpecialistAppointmentRangeView(SpecialistProfileAPIView):
    @extend_schema(
        operation_id="specialist_appointments_range",
        description=(
            "The caller's week and month boards in the admin range shape: `detail=counts` (default) up to 42 "
            "days, `detail=full` up to 7 days. The caller's profile is always listed, even as a draft."
        ),
        parameters=[SpecialistRangeQuerySerializer],
        responses={200: SpecialistAppointmentRangeSerializer, **ADMIN_ERRORS},
    )
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
        return Response(SpecialistAppointmentRangeSerializer(view, context=_context()).data)


class SpecialistAppointmentsMetaView(SpecialistProfileAPIView):
    @extend_schema(
        operation_id="specialist_appointments_meta",
        description=(
            "The admin booking-dialog meta for the caller only: their active services and active clinics. "
            "`specialists` is empty while the profile is unpublished."
        ),
        responses={200: AppointmentsMetaSerializer, **ADMIN_ERRORS},
    )
    def get(self, request: Request) -> Response:
        data = {
            "specialists": list(professionals_selectors.booking_catalogue().filter(pk=self.professional.pk)),
            "slot_step_min": services.SLOT_STEP_MIN,
        }
        return Response(AppointmentsMetaSerializer(data).data)


class SpecialistPendingSummaryView(SpecialistProfileAPIView):
    @extend_schema(
        operation_id="specialist_appointments_pending_summary",
        description="The admin pending summary over the caller's own requested appointments.",
        responses={200: PendingSummarySerializer, **ADMIN_ERRORS},
    )
    def get(self, request: Request) -> Response:
        summary = selectors.pending_summary(professional_id=self.professional.pk)
        return Response(PendingSummarySerializer(summary).data)


class ConflictsView(SpecialistProfileAPIView):
    @extend_schema(
        operation_id="specialist_conflicts",
        description=(
            "The caller's open future appointments their current schedule no longer covers, by start; "
            "reschedule or cancel them through specialist/appointments/{id}/reschedule|cancel/."
        ),
        responses={200: SpecialistConflictsSerializer, **ADMIN_ERRORS},
    )
    def get(self, request: Request) -> Response:
        conflicts = selectors.schedule_conflicts(self.professional)
        return Response(SpecialistConflictsSerializer({"results": conflicts}, context=_context()).data)


class OwnedAppointmentAPIView(SpecialistProfileAPIView):
    """Another specialist's appointment (or none) answers 404 before anything else."""

    def initial(self, request: Request, *args: Any, **kwargs: Any) -> None:
        super().initial(request, *args, **kwargs)
        if not self.professional.appointments.filter(pk=kwargs["pk"]).exists():
            raise NotFound


class SpecialistAppointmentActionView(OwnedAppointmentAPIView):
    @extend_schema(
        operation_id="specialist_appointments_action",
        description=(
            "confirm / reject (a requested visit, before its start) or attend (a confirmed visit, from its "
            "start on); otherwise 409 invalid_transition."
        ),
        request=None,
        parameters=[ACTION_PARAM, IDEMPOTENCY_KEY_HEADER],
        responses={200: SpecialistAppointmentItemSerializer, **ADMIN_ERRORS},
    )
    @idempotent()
    def post(self, request: Request, pk: int, action: str) -> Response:
        if action not in enums.AppointmentAction.values:
            raise NotFound
        services.transition(
            pk, action, actor=request.user, request=request, specialist_account_id=self.account_id
        )
        return Response(_item(pk))


class SpecialistAppointmentRescheduleView(OwnedAppointmentAPIView):
    @extend_schema(
        operation_id="specialist_appointments_reschedule",
        description=(
            "Move an own open appointment before its start (same slot rules as staff). Moving it to another "
            "clinic needs `customer_agreed`."
        ),
        request=SpecialistRescheduleSerializer,
        parameters=[IDEMPOTENCY_KEY_HEADER],
        responses={200: SpecialistAppointmentItemSerializer, **ADMIN_ERRORS},
    )
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
        return Response(_item(pk))


class SpecialistAppointmentCancelView(OwnedAppointmentAPIView):
    @extend_schema(
        operation_id="specialist_appointments_cancel",
        description="Cancel an own open appointment before its start; the customer is notified.",
        request=AppointmentCancelSerializer,
        parameters=[IDEMPOTENCY_KEY_HEADER],
        responses={200: SpecialistAppointmentItemSerializer, **ADMIN_ERRORS},
    )
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
        return Response(_item(pk))
