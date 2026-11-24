"""The shapes of the specialist's appointments board: the customer reference, the item, the day and range
boards, the conflicts and the booking bodies."""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from glowderma.appointments import services
from glowderma.appointments.api.admin.serializers import MESSAGES as ADMIN_MESSAGES
from glowderma.appointments.api.admin.serializers import (
    AppointmentItemSerializer,
    AppointmentRangeSerializer,
    DayScheduleSerializer,
    RangeQuerySerializer,
    customer_by_phone,
)
from glowderma.appointments.models import Appointment
from glowderma.core.api.exceptions import field_error
from glowderma.core.api.fields import AwareDateTimeField, LenientPhoneField
from glowderma.professionals.models import Professional, Service


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


class SpecialistAppointmentCreateSerializer(serializers.Serializer[dict[str, Any]]):
    """``POST specialist/appointments/``: a booking in the caller's own calendar. No ``customer_id`` (a
    specialist cannot address customers by id) and no note (the staff note is never shown to a specialist).
    A ``phone`` no customer holds books a walk-in: ``name`` is then required and the customer is created with
    the booking (no password, no consent: they sign in later with an SMS code to that phone)."""

    service_id = serializers.IntegerField(help_text="One of the caller's own active services.")
    location_id = serializers.IntegerField(
        min_value=1, max_value=2_147_483_647, help_text="One of the caller's active assigned clinics."
    )
    phone = LenientPhoneField()
    name = serializers.CharField(
        min_length=2,
        max_length=150,
        required=False,
        help_text="The new customer's name, required when no customer has the phone (a walk-in).",
    )
    start = AwareDateTimeField()

    def validate_service_id(self, value: int) -> Service:
        professional: Professional = self.context["professional"]
        service = professional.services.filter(pk=value, is_active=True).first()
        if service is None:
            raise serializers.ValidationError(
                [field_error("does_not_exist", ADMIN_MESSAGES["service_not_found"])]
            )
        return service

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        attrs["customer"] = customer_by_phone(attrs.pop("phone"), attrs.pop("name", ""))
        attrs["service"] = attrs.pop("service_id")
        return attrs


class SpecialistRescheduleSerializer(serializers.Serializer[dict[str, Any]]):
    start = AwareDateTimeField()
    location_id = serializers.IntegerField(required=False, min_value=1, max_value=2_147_483_647)
    customer_agreed = serializers.BooleanField(required=False, default=False)


__all__ = [
    "SpecialistAppointmentCreateSerializer",
    "SpecialistAppointmentDaySerializer",
    "SpecialistAppointmentItemSerializer",
    "SpecialistAppointmentRangeSerializer",
    "SpecialistConflictsSerializer",
    "SpecialistCustomerRefSerializer",
    "SpecialistRangeQuerySerializer",
    "SpecialistRescheduleSerializer",
]
