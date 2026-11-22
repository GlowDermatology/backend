"""Admin serializers of `appointments` (contract §2.9 and the T10 admin create / reschedule / cancel)."""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from glowderma.accounts.models import Customer
from glowderma.appointments.models import Appointment
from glowderma.appointments.selectors import (
    RANGE_MAX_DAYS_COUNTS,
    RANGE_MAX_DAYS_FULL,
    DayView,
    RangeDay,
    RangeSchedule,
    RangeView,
)
from glowderma.appointments.services import WalkIn
from glowderma.core import enums
from glowderma.core.api.exceptions import field_error
from glowderma.core.api.fields import AwareDateTimeField, LenientPhoneField
from glowderma.core.api.serializers import CustomerRefSerializer
from glowderma.core.uzsource import N_
from glowderma.professionals.models import Location, Professional, Service
from glowderma.professionals.selectors import DaySchedule, assigned_locations

MESSAGES = {
    "specialist_not_found": N_("Mutaxassis topilmadi."),
    "service_not_found": N_("Xizmat topilmadi."),
    "customer_not_found": N_("Bu raqam bilan mijoz topilmadi."),
    "walk_in_name": N_("Bu raqam bilan mijoz topilmadi: yangi mijozning ismini kiriting."),
    "customer_inactive": N_("Bu raqamdagi mijoz hisobi faol emas."),
    "customer_id_not_found": N_("Mijoz topilmadi."),
    "one_customer_ref": N_("customer_id yoki phone dan faqat bittasini yuboring."),
}


class AppointmentClinicSerializer(serializers.Serializer[Location]):
    id = serializers.IntegerField()
    name = serializers.CharField(source="name_uz")
    address = serializers.CharField(source="address_uz", allow_blank=True)


class AppointmentItemSerializer(serializers.Serializer[Appointment]):
    """The day-item shape (contract §2.9), also returned by every action endpoint."""

    id = serializers.IntegerField()
    specialist_id = serializers.IntegerField(source="professional_id")
    customer = CustomerRefSerializer()
    service_id = serializers.IntegerField()
    service_name = serializers.CharField(source="service.name_uz")
    service_short_name = serializers.CharField(source="service.short_name_uz")
    location = AppointmentClinicSerializer()
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
    # false for a deactivated specialist still listed for the appointments it has that day
    is_active = serializers.BooleanField(source="professional.is_active")
    working_hours = serializers.SerializerMethodField()
    location = AppointmentClinicSerializer(allow_null=True)
    blocks = DayBlockSerializer(many=True)

    @extend_schema_field(WorkingHoursSerializer(allow_null=True))
    def get_working_hours(self, obj: DaySchedule) -> dict[str, str] | None:
        if obj.working_hours is None:
            return None
        return {"start": obj.working_hours[0], "end": obj.working_hours[1]}


class ScheduleConflictsSerializer(serializers.Serializer[dict[str, Any]]):
    """``GET specialists/{id}/conflicts/``: open future appointments the schedule no longer covers."""

    results = AppointmentItemSerializer(many=True)


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
    """``POST appointments/`` (T10): ``specialist_id``, ``service_id`` (one of that specialist's active
    services), ``customer_id`` | ``phone``, ``start`` (aware, 5-minute grid, inside working hours), ``note``
    (≤ 300). A ``phone`` no customer holds books a walk-in: ``name`` is then required and the customer is
    created with the booking (no password, no consent: they sign in later with an SMS code to that phone)."""

    specialist_id = serializers.IntegerField()
    service_id = serializers.IntegerField()
    location_id = serializers.IntegerField(min_value=1, max_value=2_147_483_647)
    customer_id = serializers.IntegerField(required=False)
    phone = LenientPhoneField(required=False)
    name = serializers.CharField(
        min_length=2,
        max_length=150,
        required=False,
        help_text="The new customer's name, required when no customer has the phone (a walk-in).",
    )
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
        return _active_service(value)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        attrs.setdefault("note", "")
        name = attrs.pop("name", "")
        has_id, has_phone = "customer_id" in attrs, "phone" in attrs
        if has_id == has_phone:
            code = "required" if not has_id else "invalid"
            message = None if not has_id else MESSAGES["one_customer_ref"]
            raise serializers.ValidationError({"customer_id": [field_error(code, message)]})
        active = Customer.objects.filter(status=enums.CustomerStatus.ACTIVE)
        customer: Customer | WalkIn | None
        if has_id:
            customer = active.filter(pk=attrs.pop("customer_id")).first()
            if customer is None:
                raise serializers.ValidationError(
                    {"customer_id": [field_error("does_not_exist", MESSAGES["customer_id_not_found"])]}
                )
        else:
            phone = attrs.pop("phone")
            customer = active.filter(phone=phone).first()
            if customer is None and Customer.objects.filter(phone=phone).exists():
                raise serializers.ValidationError(
                    {"phone": [field_error("customer_not_found", MESSAGES["customer_inactive"])]}
                )
            if customer is None:
                if not name:
                    raise serializers.ValidationError(
                        {"name": [field_error("required", MESSAGES["walk_in_name"])]}
                    )
                customer = WalkIn(phone=phone, name=name)
        attrs["customer"] = customer
        attrs["professional"] = attrs.pop("specialist_id")
        attrs["service"] = attrs.pop("service_id")
        return attrs


def _active_service(pk: int) -> Service:
    service = Service.objects.filter(pk=pk, is_active=True).first()
    if service is None:
        raise serializers.ValidationError([field_error("does_not_exist", MESSAGES["service_not_found"])])
    return service


class AppointmentRescheduleSerializer(serializers.Serializer[dict[str, Any]]):
    """``POST appointments/{id}/reschedule/``: ``start`` (same slot rules), optional ``specialist_id`` and
    ``service_id`` (required with another specialist: every service belongs to one specialist)."""

    start = AwareDateTimeField()
    location_id = serializers.IntegerField(min_value=1, max_value=2_147_483_647)
    specialist_id = serializers.IntegerField(required=False)
    service_id = serializers.IntegerField(
        required=False,
        help_text=(
            "One of the target specialist's active services; required when moving to another specialist, "
            "omitted = keep the booked service (and its duration and price)."
        ),
    )

    def validate_service_id(self, value: int) -> Service:
        return _active_service(value)

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

    id = serializers.IntegerField()
    name = serializers.CharField(source="name_uz")
    short_name = serializers.CharField(source="short_name_uz")
    duration_min = serializers.IntegerField()
    price = serializers.IntegerField()


class BookingSpecialistSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    name = serializers.CharField()
    role = serializers.ChoiceField(choices=enums.SpecialistRole.values)
    services = BookingServiceSerializer(many=True, source="bookable_services")
    locations = serializers.SerializerMethodField()

    @extend_schema_field(AppointmentClinicSerializer(many=True))
    def get_locations(self, obj: Professional) -> list[dict[str, Any]]:
        items = assigned_locations(obj, active_only=True)
        serializer = AppointmentClinicSerializer(items, many=True)  # type: ignore[arg-type]
        return list(serializer.data)


class AppointmentsMetaSerializer(serializers.Serializer[dict[str, Any]]):
    specialists = BookingSpecialistSerializer(many=True)
    slot_step_min = serializers.IntegerField()


# --- GET appointments/range/ (contract §2.9, feat/admin-ux: week and month boards) -------------------------

RANGE_MESSAGES = {
    "empty_range": N_("Oxirgi sana boshlang‘ich sanadan oldin bo‘lmasin."),
    "counts_too_long": N_("Ko‘pi bilan 42 kunlik davr."),
    "full_too_long": N_("To‘liq ko‘rinish ko‘pi bilan 7 kunlik davr uchun."),
}


class RangeQuerySerializer(serializers.Serializer[dict[str, Any]]):
    """``from`` ≤ ``to`` (Tashkent days, inclusive), at most 42 days with ``detail=counts`` (default) and 7
    with ``detail=full``; ``specialist_id`` narrows to one specialist (active or not)."""

    from_ = serializers.DateField()
    to = serializers.DateField()
    specialist_id = serializers.IntegerField(required=False, min_value=1)
    detail = serializers.ChoiceField(
        choices=enums.AppointmentRangeDetail.values,
        required=False,
        default=enums.AppointmentRangeDetail.COUNTS,
    )

    def get_fields(self) -> dict[str, serializers.Field[Any, Any, Any, Any]]:
        fields = super().get_fields()
        return {"from": fields.pop("from_"), **fields}  # ``from`` is a Python keyword; errors list it first

    def validate_specialist_id(self, value: int) -> int:
        if not Professional.objects.filter(pk=value).exists():
            raise serializers.ValidationError(
                [field_error("does_not_exist", MESSAGES["specialist_not_found"])]
            )
        return value

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        first, last = attrs["from"], attrs["to"]
        if last < first:
            raise serializers.ValidationError(
                {"to": [field_error("empty_range", RANGE_MESSAGES["empty_range"])]}
            )
        span = (last - first).days + 1
        if attrs["detail"] == enums.AppointmentRangeDetail.FULL:
            if span > RANGE_MAX_DAYS_FULL:
                raise serializers.ValidationError(
                    {"to": [field_error("out_of_range", RANGE_MESSAGES["full_too_long"])]}
                )
        elif span > RANGE_MAX_DAYS_COUNTS:
            raise serializers.ValidationError(
                {"to": [field_error("out_of_range", RANGE_MESSAGES["counts_too_long"])]}
            )
        return attrs


class RangeSpecialistSerializer(serializers.Serializer[Professional]):
    id = serializers.IntegerField()
    name = serializers.CharField()
    role = serializers.ChoiceField(choices=enums.SpecialistRole.values)
    is_active = serializers.BooleanField()


class RangeStatusCountsSerializer(serializers.Serializer[dict[str, int]]):
    requested = serializers.IntegerField()
    confirmed = serializers.IntegerField()
    attended = serializers.IntegerField()
    rejected = serializers.IntegerField()
    cancelled = serializers.IntegerField()


class RangeDaySerializer(serializers.Serializer[RangeDay]):
    date = serializers.DateField()
    counts = RangeStatusCountsSerializer()
    total = serializers.IntegerField()
    working = serializers.BooleanField(help_text="At least one listed specialist works that day.")
    hours = serializers.SerializerMethodField()

    @extend_schema_field(
        WorkingHoursSerializer(
            allow_null=True,
            help_text="Earliest start and latest end of the listed specialists working that day; null when "
            "nobody works.",
        )
    )
    def get_hours(self, obj: RangeDay) -> dict[str, str] | None:
        return None if obj.hours is None else {"start": obj.hours[0], "end": obj.hours[1]}


class RangeScheduleSerializer(serializers.Serializer[RangeSchedule]):
    specialist_id = serializers.IntegerField()
    date = serializers.DateField()
    working_hours = serializers.SerializerMethodField()
    blocks = DayBlockSerializer(many=True, source="schedule.blocks")
    location = AppointmentClinicSerializer(source="schedule.location", allow_null=True)

    @extend_schema_field(WorkingHoursSerializer(allow_null=True))
    def get_working_hours(self, obj: RangeSchedule) -> dict[str, str] | None:
        hours = obj.schedule.working_hours
        return None if hours is None else {"start": hours[0], "end": hours[1]}


class AppointmentRangeSerializer(serializers.Serializer[RangeView]):
    """``GET appointments/range/``: ``schedules`` and ``appointments`` are present (non-null) only with
    ``detail=full``."""

    from_ = serializers.DateField(source="first")
    to = serializers.DateField(source="last")
    specialists = RangeSpecialistSerializer(many=True)
    days = RangeDaySerializer(many=True)
    schedules = RangeScheduleSerializer(many=True, allow_null=True)
    appointments = AppointmentItemSerializer(many=True, allow_null=True)

    def get_fields(self) -> dict[str, serializers.Field[Any, Any, Any, Any]]:
        fields = super().get_fields()
        return {"from": fields.pop("from_"), **fields}
