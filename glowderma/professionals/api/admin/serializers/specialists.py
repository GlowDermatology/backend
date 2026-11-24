"""The admin specialist (``ClinicSpecialist``): the row with its account, services and week, the list query
and the writes."""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from glowderma.core import enums, slugs
from glowderma.core.api.exceptions import field_error
from glowderma.core.api.fields import (
    QueryBooleanField,
    i18n_dict,
    i18n_text,
)
from glowderma.core.api.serializers import ActorSerializer
from glowderma.core.media import absolute_media_url, optional_variant_url
from glowderma.core.uzsource import N_
from glowderma.professionals import selectors
from glowderma.professionals.api.admin.serializers.clinic_services import ClinicServiceSerializer
from glowderma.professionals.api.admin.serializers.fields import MAX_POSITION, SLOT_STEPS, id_field
from glowderma.professionals.api.admin.serializers.messages import MESSAGES
from glowderma.professionals.api.admin.serializers.texts import SpecialistTextSerializer
from glowderma.professionals.api.admin.serializers.weekly_hours import WorkingDaySerializer
from glowderma.professionals.models import (
    Location,
    Professional,
)

PUBLISH_BLOCKERS = ("account", "name", "discipline", "bio", "photo", "clinic", "service", "availability")


class SpecialistAccountSerializer(ActorSerializer):
    """The owner account of a profile: its login, its current role and whether it may sign in."""

    email = serializers.EmailField()
    role = serializers.ChoiceField(choices=enums.StaffRole.values)
    is_active = serializers.BooleanField()


class ClinicSpecialistSerializer(serializers.Serializer[Professional]):
    """The admin ``ClinicSpecialist``; instances come from ``selectors.specialist_queryset()``.

    ``upcoming_appointments`` (``requested`` / ``confirmed`` appointments starting now or later) comes from
    the view's context (``upcoming``), counted once per page.
    """

    id = serializers.IntegerField()
    slug = serializers.CharField(help_text="The website URL segment (B-W1): unique, editable.")
    name = serializers.CharField()
    role = serializers.ChoiceField(
        choices=enums.SpecialistRole.values, allow_blank=True, help_text='`""` while a draft has none.'
    )
    qualifications = serializers.SerializerMethodField()
    bio = serializers.SerializerMethodField()
    experience_years = serializers.IntegerField(allow_null=True)
    photo_url = serializers.SerializerMethodField()
    photo_thumbnail_url = serializers.SerializerMethodField(help_text="400 px WebP variant.")
    photo_medium_url = serializers.SerializerMethodField(help_text="1000 px WebP variant.")
    location_id = serializers.IntegerField(allow_null=True, help_text="`null` while a draft has no clinic.")
    location_ids = serializers.SerializerMethodField()
    account = SpecialistAccountSerializer(source="staff_user", help_text="The account that owns the profile.")
    publish_blockers = serializers.SerializerMethodField(
        help_text=(
            "What keeps the profile from being published (`[]` = ready): account, name, discipline, bio, "
            "photo, clinic, service, availability."
        )
    )
    slot_step_min = serializers.IntegerField()
    position = serializers.IntegerField()
    is_active = serializers.BooleanField()
    services = ClinicServiceSerializer(
        many=True, source="service_rows", help_text="Every own service, inactive ones included, by position."
    )
    working_hours = WorkingDaySerializer(many=True, source="week_rows")
    upcoming_appointments = serializers.SerializerMethodField()
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()

    @extend_schema_field(serializers.ListField(child=serializers.IntegerField()))
    def get_location_ids(self, obj: Professional) -> list[int]:
        return selectors.assigned_location_ids(obj)

    @extend_schema_field(i18n_text(2000))
    def get_qualifications(self, obj: Professional) -> dict[str, str]:
        return i18n_dict(obj, "qualifications")

    @extend_schema_field(i18n_text(2000))
    def get_bio(self, obj: Professional) -> dict[str, str]:
        return i18n_dict(obj, "bio")

    @extend_schema_field(serializers.URLField(allow_null=True))
    def get_photo_url(self, obj: Professional) -> str | None:
        return absolute_media_url(obj.photo.name or "") if obj.photo else None

    @extend_schema_field(serializers.URLField(allow_null=True))
    def get_photo_thumbnail_url(self, obj: Professional) -> str | None:
        return optional_variant_url(obj.photo.name, "thumbnail") if obj.photo else None

    @extend_schema_field(serializers.URLField(allow_null=True))
    def get_photo_medium_url(self, obj: Professional) -> str | None:
        return optional_variant_url(obj.photo.name, "medium") if obj.photo else None

    def get_upcoming_appointments(self, obj: Professional) -> int:
        return int(self.context.get("upcoming", {}).get(obj.pk, 0))

    @extend_schema_field(serializers.ListField(child=serializers.ChoiceField(choices=PUBLISH_BLOCKERS)))
    def get_publish_blockers(self, obj: Professional) -> list[str]:
        return selectors.publish_blockers(obj)


class SpecialistListQuerySerializer(serializers.Serializer[dict[str, Any]]):
    """Query of ``GET specialists/``; ``search`` is trimmed and cut to 64 characters."""

    search = serializers.CharField(required=False, allow_blank=True, trim_whitespace=False)
    is_active = QueryBooleanField(required=False)
    role = serializers.ChoiceField(choices=enums.SpecialistRole.values, required=False)
    ordering = serializers.ChoiceField(
        choices=enums.DirectoryOrdering.values, required=False, default=enums.DirectoryOrdering.POSITION
    )


class ClinicSpecialistWriteSerializer(serializers.Serializer[dict[str, Any]]):
    """Fields staff edit on a profile (validated ``partial``, see the subclass ``PATCH specialists/{id}/``).

    ``context["professional_id"]`` is the edited specialist. The owner account is never part of the body: a
    profile is created with its account and stays attached to it.
    """

    name = serializers.CharField(min_length=2, max_length=150)
    role = serializers.ChoiceField(choices=enums.SpecialistRole.values)
    qualifications = SpecialistTextSerializer(required=False)
    bio = SpecialistTextSerializer(required=False)
    experience_years = serializers.IntegerField(min_value=0, max_value=80, allow_null=True, required=False)
    slot_step_min = serializers.IntegerField(
        required=False, help_text="The customer slot grid in minutes: 10, 15, 20, 30 (default) or 60."
    )
    location_id = id_field(help_text="The primary clinic, which must be included in location_ids.")
    location_ids = serializers.ListField(child=id_field(), allow_empty=False)
    position = serializers.IntegerField(min_value=0, max_value=MAX_POSITION, required=False)
    is_active = serializers.BooleanField(
        required=False,
        help_text="Published: listed on the website and bookable. Publishing needs a complete profile.",
    )
    slug = serializers.CharField(
        max_length=160, required=False, help_text="Optional on create (derived from the name)."
    )

    def validate_slug(self, value: str) -> str:
        """B-W1: ``^[a-z0-9]+(-[a-z0-9]+)*$``, not only digits, ≤160, unique."""
        value = value.strip()
        if not slugs.is_valid_slug(value, max_length=160):
            raise serializers.ValidationError(
                field_error(
                    "invalid", N_("Faqat lotin kichik harflari, raqamlar va chiziqcha (faqat raqam emas).")
                )
            )
        taken = Professional.objects.filter(slug=value)
        editing = self.context.get("professional_id")
        if editing is not None:
            taken = taken.exclude(pk=editing)
        if taken.exists():
            raise serializers.ValidationError(
                field_error("unique", N_("Bu manzil (slug) boshqa mutaxassisda bor."))
            )
        return value

    def validate_slot_step_min(self, value: int) -> int:
        if value not in SLOT_STEPS:
            raise serializers.ValidationError(field_error("invalid_choice", MESSAGES["slot_step"]))
        return value

    def validate_location_id(self, location_id: int) -> int:
        if not Location.objects.filter(pk=location_id).exists():
            raise serializers.ValidationError(field_error("does_not_exist", N_("Manzil topilmadi.")))
        return location_id

    def validate_location_ids(self, ids: list[int]) -> list[int]:
        if len(ids) != len(set(ids)):
            raise serializers.ValidationError(field_error("unique"))
        return ids


class ClinicSpecialistPatchSerializer(ClinicSpecialistWriteSerializer):
    """``PATCH specialists/{id}/`` (validated with ``partial=True``): every field optional; ``qualifications``
    and ``bio`` merge leaf by leaf; ``force`` confirms a deactivation despite upcoming appointments."""

    force = serializers.BooleanField(
        required=False,
        default=False,
        help_text="With is_active=false: deactivate even with requested/confirmed appointments ahead.",
    )


__all__ = [
    "PUBLISH_BLOCKERS",
    "ClinicSpecialistPatchSerializer",
    "ClinicSpecialistSerializer",
    "ClinicSpecialistWriteSerializer",
    "SpecialistAccountSerializer",
    "SpecialistListQuerySerializer",
]
