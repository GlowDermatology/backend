"""Shapes of the customer appointment routes (customer contract §2.11).

Every i18n field is rendered as one string in the resolved language (``context["lang"]``; fallback the Uzbek
text, then ``""``); enum values come with their ``*_label`` (§5.3).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers
from rest_framework.exceptions import ValidationError

from glowderma.appointments import selectors
from glowderma.appointments.models import Appointment
from glowderma.appointments.services import ACCOUNT_DELETED_REASON
from glowderma.core import enums, i18n, timeutils
from glowderma.core.api.exceptions import field_error
from glowderma.core.media import absolute_media_url
from glowderma.professionals.models import Location, Professional, ProfessionalService


def pick(obj: Any, prefix: str, lang: str) -> str:
    """``<prefix>_<lang>`` → ``<prefix>_uz`` → ``""`` (customer contract §1.4)."""
    return str(getattr(obj, f"{prefix}_{lang}", "") or getattr(obj, f"{prefix}_uz", "") or "")


def _lang(serializer: serializers.Serializer[Any]) -> str:
    return i18n.normalize_language(serializer.context.get("lang"))


def _photo_url(professional: Professional) -> str | None:
    return absolute_media_url(professional.photo.name or "") if professional.photo else None


def _coordinate(value: Any) -> float | None:
    return None if value is None else round(float(value), 6)


class LocationSerializer(serializers.Serializer[Location]):
    id = serializers.IntegerField()
    name = serializers.SerializerMethodField()
    address = serializers.SerializerMethodField()
    phone = serializers.SerializerMethodField()
    latitude = serializers.SerializerMethodField()
    longitude = serializers.SerializerMethodField()

    def get_name(self, obj: Location) -> str:
        return pick(obj, "name", _lang(self))

    def get_address(self, obj: Location) -> str:
        return pick(obj, "address", _lang(self))

    def get_phone(self, obj: Location) -> str | None:
        return obj.phone or None

    def get_latitude(self, obj: Location) -> float | None:
        return _coordinate(obj.latitude)

    def get_longitude(self, obj: Location) -> float | None:
        return _coordinate(obj.longitude)


class SpecialistServiceSerializer(serializers.Serializer[ProfessionalService]):
    id = serializers.IntegerField(source="service_id")
    name = serializers.SerializerMethodField()
    short_name = serializers.SerializerMethodField()
    duration_min = serializers.IntegerField()
    price = serializers.IntegerField()

    def get_name(self, obj: ProfessionalService) -> str:
        return pick(obj.service, "name", _lang(self))

    def get_short_name(self, obj: ProfessionalService) -> str:
        return pick(obj.service, "short_name", _lang(self))


class SpecialistSerializer(serializers.Serializer[Professional]):
    id = serializers.IntegerField()
    name = serializers.CharField()
    role = serializers.ChoiceField(choices=enums.SpecialistRole.values)
    role_label = serializers.SerializerMethodField()
    photo_url = serializers.SerializerMethodField()
    bio = serializers.SerializerMethodField()
    experience_years = serializers.IntegerField(allow_null=True)
    location = LocationSerializer()
    services = serializers.SerializerMethodField()
    slot_step_min = serializers.IntegerField()

    def get_role_label(self, obj: Professional) -> str:
        return i18n.t(f"label.role.{obj.role}", _lang(self))

    def get_photo_url(self, obj: Professional) -> str | None:
        return _photo_url(obj)

    def get_bio(self, obj: Professional) -> str:
        return pick(obj, "bio", _lang(self))

    @extend_schema_field(SpecialistServiceSerializer(many=True))
    def get_services(self, obj: Professional) -> list[dict[str, Any]]:
        links = selectors.bookable_links(obj)
        serializer: Any = SpecialistServiceSerializer(links, many=True, context=self.context)  # type: ignore[arg-type]
        return list(serializer.data)


class SpecialistListSerializer(serializers.Serializer[Any]):
    results = SpecialistSerializer(many=True)


class ServiceOfferSerializer(serializers.Serializer[selectors.ServiceOffer]):
    id = serializers.IntegerField(source="service.pk")
    name = serializers.SerializerMethodField()
    short_name = serializers.SerializerMethodField()
    description = serializers.SerializerMethodField()
    duration_min = serializers.IntegerField(
        source="service.default_duration_min", help_text="Default duration."
    )
    price_from = serializers.IntegerField()
    price_to = serializers.IntegerField()
    specialist_ids = serializers.ListField(child=serializers.IntegerField())

    def get_name(self, obj: selectors.ServiceOffer) -> str:
        return pick(obj.service, "name", _lang(self))

    def get_short_name(self, obj: selectors.ServiceOffer) -> str:
        return pick(obj.service, "short_name", _lang(self))

    def get_description(self, obj: selectors.ServiceOffer) -> str:
        return pick(obj.service, "description", _lang(self))


class ServiceListSerializer(serializers.Serializer[Any]):
    results = ServiceOfferSerializer(many=True)


class SlotsQuerySerializer(serializers.Serializer[dict[str, Any]]):
    date = serializers.DateField(help_text="Tashkent calendar day, today … today + BOOKING_HORIZON_DAYS.")
    service_id = serializers.IntegerField(min_value=1, max_value=2_147_483_647)


class SlotWorkingHoursSerializer(serializers.Serializer[Any]):
    start = serializers.CharField()
    end = serializers.CharField()


class SlotSerializer(serializers.Serializer[Any]):
    start = serializers.CharField(help_text='"HH:MM" Tashkent.')
    end = serializers.CharField(help_text='"HH:MM" Tashkent.')
    start_at = serializers.DateTimeField()


class FreeSlotsSerializer(serializers.Serializer[Any]):
    date = serializers.DateField()
    specialist_id = serializers.IntegerField()
    service_id = serializers.IntegerField()
    duration_min = serializers.IntegerField()
    price = serializers.IntegerField()
    slot_step_min = serializers.IntegerField()
    working_hours = SlotWorkingHoursSerializer(allow_null=True)
    slots = SlotSerializer(many=True)


class AnyOffsetDateTimeField(serializers.DateTimeField):
    """An ISO datetime kept as submitted: a naive value is refused later, at its place in the booking table
    (C§2.11 row "start aware" → ``invalid``)."""

    def enforce_timezone(self, value: datetime) -> datetime:
        return value


class BookingSerializer(serializers.Serializer[dict[str, Any]]):
    specialist_id = serializers.IntegerField(min_value=1, max_value=2_147_483_647)
    service_id = serializers.IntegerField(min_value=1, max_value=2_147_483_647)
    start = AnyOffsetDateTimeField()
    note = serializers.CharField(max_length=300, allow_null=True, allow_blank=True, required=False)


class CancelSerializer(serializers.Serializer[dict[str, Any]]):
    reason = serializers.CharField(max_length=300, allow_null=True, allow_blank=True, required=False)

    def validate_reason(self, value: str | None) -> str | None:
        """``account_deleted`` is the reason ``DELETE me/`` records (C§2.2.3); a customer cannot claim it, so
        the event and the audit never show a false account deletion."""
        if value is not None and value.strip() == ACCOUNT_DELETED_REASON:
            raise ValidationError(field_error("invalid"))
        return value


class AppointmentSpecialistSerializer(serializers.Serializer[Professional]):
    id = serializers.IntegerField()
    name = serializers.CharField()
    role = serializers.ChoiceField(choices=enums.SpecialistRole.values)
    role_label = serializers.SerializerMethodField()
    photo_url = serializers.SerializerMethodField()

    def get_role_label(self, obj: Professional) -> str:
        return i18n.t(f"label.role.{obj.role}", _lang(self))

    def get_photo_url(self, obj: Professional) -> str | None:
        return _photo_url(obj)


class AppointmentServiceSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    name = serializers.SerializerMethodField()
    short_name = serializers.SerializerMethodField()

    def get_name(self, obj: Any) -> str:
        return pick(obj, "name", _lang(self))

    def get_short_name(self, obj: Any) -> str:
        return pick(obj, "short_name", _lang(self))


class AppointmentLocationSerializer(serializers.Serializer[Location]):
    name = serializers.SerializerMethodField()
    address = serializers.SerializerMethodField()
    phone = serializers.SerializerMethodField()

    def get_name(self, obj: Location) -> str:
        return pick(obj, "name", _lang(self))

    def get_address(self, obj: Location) -> str:
        return pick(obj, "address", _lang(self))

    def get_phone(self, obj: Location) -> str | None:
        return obj.phone or None


class CustomerAppointmentSerializer(serializers.Serializer[Appointment]):
    """C§2.11 ``Appointment`` of the customer audience (``note`` is the customer's own note)."""

    id = serializers.IntegerField()
    status = serializers.ChoiceField(choices=enums.AppointmentStatus.values)
    status_label = serializers.SerializerMethodField()
    specialist = AppointmentSpecialistSerializer(source="professional")
    service = AppointmentServiceSerializer()
    location = AppointmentLocationSerializer(source="professional.location")
    start = serializers.DateTimeField()
    end = serializers.DateTimeField()
    duration_min = serializers.IntegerField()
    price = serializers.IntegerField()
    note = serializers.SerializerMethodField()
    requested_at = serializers.DateTimeField()
    confirmed_at = serializers.DateTimeField(allow_null=True)
    cancelled_at = serializers.DateTimeField(allow_null=True)
    cancelled_by = serializers.SerializerMethodField()
    can_cancel = serializers.SerializerMethodField()

    def get_status_label(self, obj: Appointment) -> str:
        return i18n.t(f"label.appointment_status.{obj.status}", _lang(self))

    def get_note(self, obj: Appointment) -> str | None:
        return obj.customer_note or None

    @extend_schema_field(
        serializers.ChoiceField(choices=enums.AppointmentCancelledBy.values, allow_null=True)
    )
    def get_cancelled_by(self, obj: Appointment) -> str | None:
        return obj.cancelled_by or None

    def get_can_cancel(self, obj: Appointment) -> bool:
        return selectors.can_cancel(obj, self.context.get("now") or timeutils.now())
