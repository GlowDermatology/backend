"""Admin serializers of `appointments` (contract §2.9 and the T10 admin create / reschedule / cancel)."""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from glowderma.accounts.models import Customer
from glowderma.appointments.models import Appointment
from glowderma.appointments.selectors import DayView
from glowderma.core import enums
from glowderma.core.api.exceptions import field_error
from glowderma.core.api.fields import AwareDateTimeField, PhoneField
from glowderma.core.api.serializers import CustomerRefSerializer
from glowderma.professionals.models import Professional, Service
from glowderma.professionals.selectors import DaySchedule

MESSAGES = {
    "specialist_not_found": "Mutaxassis topilmadi.",
    "service_not_found": "Xizmat topilmadi.",
    "customer_not_found": "Bu raqam bilan mijoz topilmadi.",
    "customer_id_not_found": "Mijoz topilmadi.",
    "one_customer_ref": "customer_id yoki phone dan faqat bittasini yuboring.",
}


class AppointmentItemSerializer(serializers.Serializer[Appointment]):
    """The day-item shape (contract §2.9), also returned by every action endpoint."""

    id = serializers.IntegerField()
    specialist_id = serializers.IntegerField(source="professional_id")
    customer = CustomerRefSerializer()
    service_id = serializers.IntegerField()
    service_name = serializers.CharField(source="service.name_uz")
    service_short_name = serializers.CharField(source="service.short_name_uz")
    duration_min = serializers.IntegerField()
    start = serializers.DateTimeField()
    end = serializers.DateTimeField()
    price = serializers.IntegerField()
    status = serializers.ChoiceField(choices=enums.AppointmentStatus.values)
    requested_at = serializers.DateTimeField()
    requested_via = serializers.ChoiceField(choices=enums.RequestedVia.values)


class WorkingHoursSerializer(serializers.Serializer[tuple[str, str]]):
    start = serializers.CharField()
    end = serializers.CharField()


class DayBlockSerializer(serializers.Serializer[Any]):
    start = serializers.CharField()
    end = serializers.CharField()
    kind = serializers.ChoiceField(choices=enums.BlockKind.values)
    label = serializers.CharField(allow_null=True)  # type: ignore[assignment]  # a field, not Field.label


class SpecialistDaySerializer(serializers.Serializer[DaySchedule]):
    id = serializers.IntegerField(source="professional.pk")
    name = serializers.CharField(source="professional.name")
    role = serializers.ChoiceField(choices=enums.SpecialistRole.values, source="professional.role")
    working_hours = serializers.SerializerMethodField()
    blocks = DayBlockSerializer(many=True)

    @extend_schema_field(WorkingHoursSerializer(allow_null=True))
    def get_working_hours(self, obj: DaySchedule) -> dict[str, str] | None:
        if obj.working_hours is None:
            return None
        return {"start": obj.working_hours[0], "end": obj.working_hours[1]}


class DayScheduleSerializer(serializers.Serializer[DayView]):
    """``GET appointments/?date=`` (contract §2.9)."""

    date = serializers.DateField()
    specialists = SpecialistDaySerializer(many=True)
    appointments = AppointmentItemSerializer(many=True)


class DayQuerySerializer(serializers.Serializer[dict[str, Any]]):
    date = serializers.DateField(required=False)


class PendingSummarySerializer(serializers.Serializer[dict[str, Any]]):
    count = serializers.IntegerField()
    next_at = serializers.DateTimeField(allow_null=True)


class AppointmentCreateSerializer(serializers.Serializer[dict[str, Any]]):
    """``POST appointments/`` (T10): ``specialist_id``, ``service_id``, ``customer_id`` | ``phone`` (E.164 of
    an active customer), ``start`` (aware, 5-minute grid, inside working hours), ``note`` (≤ 300)."""

    specialist_id = serializers.IntegerField()
    service_id = serializers.IntegerField()
    customer_id = serializers.IntegerField(required=False)
    phone = PhoneField(required=False)
    start = AwareDateTimeField()
    # Optional in the contract: no schema ``default`` (a generator would type the key as always present);
    # ``validate`` supplies the runtime default "".
    note = serializers.CharField(max_length=300, allow_blank=True, required=False)

    def validate_specialist_id(self, value: int) -> Professional:
        professional = Professional.objects.filter(pk=value, is_active=True).first()
        if professional is None:
            raise serializers.ValidationError(
                [field_error("does_not_exist", MESSAGES["specialist_not_found"])]
            )
        return professional

    def validate_service_id(self, value: int) -> Service:
        service = Service.objects.filter(pk=value, is_active=True).first()
        if service is None:
            raise serializers.ValidationError([field_error("does_not_exist", MESSAGES["service_not_found"])])
        return service

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        attrs.setdefault("note", "")
        has_id, has_phone = "customer_id" in attrs, "phone" in attrs
        if has_id == has_phone:
            code = "required" if not has_id else "invalid"
            message = None if not has_id else MESSAGES["one_customer_ref"]
            raise serializers.ValidationError({"customer_id": [field_error(code, message)]})
        active = Customer.objects.filter(status=enums.CustomerStatus.ACTIVE)
        if has_id:
            customer = active.filter(pk=attrs.pop("customer_id")).first()
            if customer is None:
                raise serializers.ValidationError(
                    {"customer_id": [field_error("does_not_exist", MESSAGES["customer_id_not_found"])]}
                )
        else:
            customer = active.filter(phone=attrs.pop("phone")).first()
            if customer is None:
                raise serializers.ValidationError(
                    {"phone": [field_error("customer_not_found", MESSAGES["customer_not_found"])]}
                )
        attrs["customer"] = customer
        attrs["professional"] = attrs.pop("specialist_id")
        attrs["service"] = attrs.pop("service_id")
        return attrs


class AppointmentRescheduleSerializer(serializers.Serializer[dict[str, Any]]):
    """``POST appointments/{id}/reschedule/``: ``start`` (same slot rules), optional ``specialist_id``."""

    start = AwareDateTimeField()
    specialist_id = serializers.IntegerField(required=False)

    def validate_specialist_id(self, value: int) -> Professional:
        professional = Professional.objects.filter(pk=value, is_active=True).first()
        if professional is None:
            raise serializers.ValidationError(
                [field_error("does_not_exist", MESSAGES["specialist_not_found"])]
            )
        return professional


class AppointmentCancelSerializer(serializers.Serializer[dict[str, Any]]):
    """``POST appointments/{id}/cancel/``: optional ``reason`` (≤ 300), kept as the staff note."""

    reason = serializers.CharField(max_length=300, allow_blank=True, required=False)  # no schema default

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        attrs.setdefault("reason", "")
        return attrs


class BookingServiceSerializer(serializers.Serializer[Any]):
    """One bookable service of a specialist: the snapshot an admin booking would take (contract §2.9 meta)."""

    id = serializers.IntegerField(source="service.pk")
    name = serializers.CharField(source="service.name_uz")
    short_name = serializers.CharField(source="service.short_name_uz")
    duration_min = serializers.IntegerField()
    price = serializers.IntegerField()


class BookingSpecialistSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    name = serializers.CharField()
    role = serializers.ChoiceField(choices=enums.SpecialistRole.values)
    services = BookingServiceSerializer(many=True, source="bookable_links")


class AppointmentsMetaSerializer(serializers.Serializer[dict[str, Any]]):
    specialists = BookingSpecialistSerializer(many=True)
    slot_step_min = serializers.IntegerField()
