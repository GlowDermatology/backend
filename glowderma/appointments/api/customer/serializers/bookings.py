"""Booking, cancelling and the customer's own appointments."""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers
from rest_framework.exceptions import ValidationError

from glowderma.appointments import selectors
from glowderma.appointments.api.customer.serializers.catalogue import LocationSerializer
from glowderma.appointments.api.customer.serializers.fields import (
    AnyOffsetDateTimeField,
    photo_url,
    pick,
    serializer_language,
)
from glowderma.appointments.models import Appointment
from glowderma.appointments.services import ACCOUNT_DELETED_REASON
from glowderma.core import enums, i18n, timeutils
from glowderma.core.api.exceptions import field_error
from glowderma.core.media import optional_variant_url
from glowderma.professionals.models import Professional


class BookingSerializer(serializers.Serializer[dict[str, Any]]):
    specialist_id = serializers.IntegerField(min_value=1, max_value=2_147_483_647)
    service_id = serializers.IntegerField(min_value=1, max_value=2_147_483_647)
    location_id = serializers.IntegerField(min_value=1, max_value=2_147_483_647)
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
    photo_thumbnail_url = serializers.SerializerMethodField(help_text="400 px WebP variant.")
    photo_medium_url = serializers.SerializerMethodField(help_text="1000 px WebP variant.")

    def get_role_label(self, obj: Professional) -> str:
        return i18n.t(f"label.role.{obj.role}", serializer_language(self))

    def get_photo_url(self, obj: Professional) -> str | None:
        return photo_url(obj)

    @extend_schema_field(serializers.URLField(allow_null=True))
    def get_photo_thumbnail_url(self, obj: Professional) -> str | None:
        return optional_variant_url(obj.photo.name, "thumbnail") if obj.photo else None

    @extend_schema_field(serializers.URLField(allow_null=True))
    def get_photo_medium_url(self, obj: Professional) -> str | None:
        return optional_variant_url(obj.photo.name, "medium") if obj.photo else None


class AppointmentServiceSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    name = serializers.SerializerMethodField()
    short_name = serializers.SerializerMethodField()

    def get_name(self, obj: Any) -> str:
        return pick(obj, "name", serializer_language(self))

    def get_short_name(self, obj: Any) -> str:
        return pick(obj, "short_name", serializer_language(self))


class CustomerAppointmentSerializer(serializers.Serializer[Appointment]):
    """C§2.11 ``Appointment`` of the customer audience (``note`` is the customer's own note)."""

    id = serializers.IntegerField()
    status = serializers.ChoiceField(choices=enums.AppointmentStatus.values)
    status_label = serializers.SerializerMethodField()
    specialist = AppointmentSpecialistSerializer(source="professional")
    service = AppointmentServiceSerializer()
    location = LocationSerializer()
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
        return i18n.t(f"label.appointment_status.{obj.status}", serializer_language(self))

    def get_note(self, obj: Appointment) -> str | None:
        return obj.customer_note or None

    @extend_schema_field(
        serializers.ChoiceField(choices=enums.AppointmentCancelledBy.values, allow_null=True)
    )
    def get_cancelled_by(self, obj: Appointment) -> str | None:
        return obj.cancelled_by or None

    def get_can_cancel(self, obj: Appointment) -> bool:
        return selectors.can_cancel(obj, self.context.get("now") or timeutils.now())


__all__ = [
    "AppointmentServiceSerializer",
    "AppointmentSpecialistSerializer",
    "BookingSerializer",
    "CancelSerializer",
    "CustomerAppointmentSerializer",
]
