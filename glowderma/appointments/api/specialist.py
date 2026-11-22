from typing import Any

from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.appointments import selectors, services
from glowderma.appointments.api.admin.serializers import (
    AppointmentCancelSerializer,
    RangeQuerySerializer,
    RangeScheduleSerializer,
)
from glowderma.appointments.models import Appointment
from glowderma.core import enums
from glowderma.core.api.fields import AwareDateTimeField
from glowderma.core.api.schema import ADMIN_ERRORS, IDEMPOTENCY_KEY_HEADER
from glowderma.core.idempotency import idempotent
from glowderma.professionals.api.specialist import SpecialistProfileAPIView
from glowderma.professionals.models import Location


class ScheduleClinicSerializer(serializers.Serializer[Location]):
    id = serializers.IntegerField()
    name = serializers.CharField(source="name_uz")
    address = serializers.CharField(source="address_uz", allow_blank=True)


class SpecialistBookingSerializer(serializers.Serializer[Appointment]):
    id = serializers.IntegerField()
    customer_name = serializers.CharField(source="customer.name", allow_blank=True)
    customer_phone = serializers.CharField(source="customer.phone", allow_null=True)
    customer_note = serializers.CharField(allow_blank=True)
    service = serializers.CharField(source="service.name_uz")
    location = ScheduleClinicSerializer()
    start = serializers.DateTimeField()
    end = serializers.DateTimeField()
    status = serializers.ChoiceField(choices=enums.AppointmentStatus.values)


class SpecialistCalendarQuerySerializer(RangeQuerySerializer):
    def get_fields(self) -> dict[str, Any]:
        fields = super().get_fields()
        del fields["specialist_id"], fields["detail"]
        return fields

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        return super().validate({**attrs, "detail": enums.AppointmentRangeDetail.FULL})


class SpecialistCalendarSerializer(serializers.Serializer[selectors.RangeView]):
    schedules = RangeScheduleSerializer(many=True)
    appointments = SpecialistBookingSerializer(many=True)


class SpecialistConflictsSerializer(serializers.Serializer[dict[str, Any]]):
    results = SpecialistBookingSerializer(many=True)


class SpecialistRescheduleSerializer(serializers.Serializer[dict[str, Any]]):
    start = AwareDateTimeField()
    location_id = serializers.IntegerField(required=False, min_value=1, max_value=2_147_483_647)
    customer_agreed = serializers.BooleanField(required=False, default=False)


class CalendarView(SpecialistProfileAPIView):
    @extend_schema(
        operation_id="specialist_calendar",
        parameters=[SpecialistCalendarQuerySerializer],
        responses={200: SpecialistCalendarSerializer, **ADMIN_ERRORS},
    )
    def get(self, request: Request) -> Response:
        body = SpecialistCalendarQuerySerializer(data=request.query_params.dict())
        body.is_valid(raise_exception=True)
        data = body.validated_data
        calendar = selectors.range_view(
            data["from"], data["to"], specialist_id=self.professional.pk, full=True
        )
        return Response(SpecialistCalendarSerializer(calendar).data)


class ConflictsView(SpecialistProfileAPIView):
    @extend_schema(
        operation_id="specialist_conflicts", responses={200: SpecialistConflictsSerializer, **ADMIN_ERRORS}
    )
    def get(self, request: Request) -> Response:
        return Response(
            SpecialistConflictsSerializer({"results": selectors.schedule_conflicts(self.professional)}).data
        )


class OwnedBookingAPIView(SpecialistProfileAPIView):
    def initial(self, request: Request, *args: Any, **kwargs: Any) -> None:
        super().initial(request, *args, **kwargs)
        if not self.professional.appointments.filter(pk=kwargs["pk"]).exists():
            raise NotFound


class BookingActionView(OwnedBookingAPIView):
    @extend_schema(
        operation_id="specialist_booking_action",
        request=None,
        parameters=[IDEMPOTENCY_KEY_HEADER],
        responses={204: None, **ADMIN_ERRORS},
    )
    @idempotent()
    def post(self, request: Request, pk: int, action: str) -> Response:
        if action not in enums.AppointmentAction.values:
            raise NotFound
        services.transition(
            pk, action, actor=request.user, request=request, specialist_account_id=self.account_id
        )
        return Response(status=204)


class BookingRescheduleView(OwnedBookingAPIView):
    @extend_schema(
        operation_id="specialist_booking_reschedule",
        request=SpecialistRescheduleSerializer,
        parameters=[IDEMPOTENCY_KEY_HEADER],
        responses={204: None, **ADMIN_ERRORS},
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
        return Response(status=204)


class BookingCancelView(OwnedBookingAPIView):
    @extend_schema(
        operation_id="specialist_booking_cancel",
        request=AppointmentCancelSerializer,
        parameters=[IDEMPOTENCY_KEY_HEADER],
        responses={204: None, **ADMIN_ERRORS},
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
        return Response(status=204)
