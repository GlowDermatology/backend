"""The booking catalogue (customer contract): clinics, specialists and their services, and the service
offers."""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from glowderma.appointments import selectors
from glowderma.appointments.api.customer.serializers.fields import (
    coordinate,
    photo_url,
    pick,
    serializer_language,
)
from glowderma.core import enums, i18n
from glowderma.core.api.fields import AwareDateTimeField
from glowderma.core.media import optional_variant_url
from glowderma.professionals.models import Location, Professional, Service
from glowderma.professionals.selectors import assigned_locations


class LocationSerializer(serializers.Serializer[Location]):
    id = serializers.IntegerField()
    name = serializers.SerializerMethodField()
    address = serializers.SerializerMethodField()
    directions = serializers.SerializerMethodField()
    phone = serializers.SerializerMethodField()
    latitude = serializers.SerializerMethodField()
    longitude = serializers.SerializerMethodField()

    def get_name(self, obj: Location) -> str:
        return pick(obj, "name", serializer_language(self))

    def get_address(self, obj: Location) -> str:
        return pick(obj, "address", serializer_language(self))

    def get_directions(self, obj: Location) -> str:
        return pick(obj, "directions", serializer_language(self))

    def get_phone(self, obj: Location) -> str | None:
        return obj.phone or None

    def get_latitude(self, obj: Location) -> float | None:
        return coordinate(obj.latitude)

    def get_longitude(self, obj: Location) -> float | None:
        return coordinate(obj.longitude)


class SpecialistServiceSerializer(serializers.Serializer[Service]):
    id = serializers.IntegerField()
    name = serializers.SerializerMethodField()
    short_name = serializers.SerializerMethodField()
    duration_min = serializers.IntegerField()
    price = serializers.IntegerField()

    def get_name(self, obj: Service) -> str:
        return pick(obj, "name", serializer_language(self))

    def get_short_name(self, obj: Service) -> str:
        return pick(obj, "short_name", serializer_language(self))


class SpecialistSerializer(serializers.Serializer[Professional]):
    id = serializers.IntegerField()
    slug = serializers.CharField(help_text="The website URL segment (B-W1); `specialists/{slug}/`.")
    name = serializers.CharField()
    content_language = serializers.SerializerMethodField(
        help_text=(
            "Language of `bio` (else of `qualifications`): the requested language, or `uz` when it fell back "
            "to Uzbek. `name` is the same in every language."
        )
    )
    role = serializers.ChoiceField(choices=enums.SpecialistRole.values)
    role_label = serializers.SerializerMethodField()
    photo_url = serializers.SerializerMethodField()
    photo_thumbnail_url = serializers.SerializerMethodField(help_text="400 px WebP variant.")
    photo_medium_url = serializers.SerializerMethodField(help_text="1000 px WebP variant.")
    bio = serializers.SerializerMethodField()
    qualifications = serializers.SerializerMethodField()
    experience_years = serializers.IntegerField(allow_null=True)
    locations = serializers.SerializerMethodField()
    services = serializers.SerializerMethodField()
    slot_step_min = serializers.IntegerField()
    updated_at = AwareDateTimeField(help_text="Last change of the specialist (sitemap `lastmod`).")

    def get_role_label(self, obj: Professional) -> str:
        return i18n.t(f"label.role.{obj.role}", serializer_language(self))

    @extend_schema_field(serializers.ChoiceField(choices=enums.ContentLanguage.values))
    def get_content_language(self, obj: Professional) -> str:
        field = (
            "bio" if obj.bio_uz or getattr(obj, f"bio_{serializer_language(self)}", "") else "qualifications"
        )
        return i18n.content_language(obj, field, serializer_language(self))

    def get_photo_url(self, obj: Professional) -> str | None:
        return photo_url(obj)

    @extend_schema_field(serializers.URLField(allow_null=True))
    def get_photo_thumbnail_url(self, obj: Professional) -> str | None:
        return optional_variant_url(obj.photo.name, "thumbnail") if obj.photo else None

    @extend_schema_field(serializers.URLField(allow_null=True))
    def get_photo_medium_url(self, obj: Professional) -> str | None:
        return optional_variant_url(obj.photo.name, "medium") if obj.photo else None

    def get_bio(self, obj: Professional) -> str:
        return pick(obj, "bio", serializer_language(self))

    def get_qualifications(self, obj: Professional) -> str:
        return pick(obj, "qualifications", serializer_language(self))

    @extend_schema_field(LocationSerializer(many=True))
    def get_locations(self, obj: Professional) -> list[dict[str, Any]]:
        items = assigned_locations(obj, active_only=True)
        serializer = LocationSerializer(items, many=True, context=self.context)  # type: ignore[arg-type]
        return list(serializer.data)

    @extend_schema_field(SpecialistServiceSerializer(many=True))
    def get_services(self, obj: Professional) -> list[dict[str, Any]]:
        rows = selectors.bookable_services(obj)
        serializer: Any = SpecialistServiceSerializer(rows, many=True, context=self.context)  # type: ignore[arg-type]
        return list(serializer.data)


class SpecialistListSerializer(serializers.Serializer[Any]):
    results = SpecialistSerializer(many=True)


class ServiceOfferSerializer(serializers.Serializer[selectors.ServiceOffer]):
    id = serializers.IntegerField(source="service.pk")
    name = serializers.SerializerMethodField()
    short_name = serializers.SerializerMethodField()
    description = serializers.SerializerMethodField()
    category = serializers.ChoiceField(choices=enums.ServiceCategory.values, source="service.category")
    category_label = serializers.SerializerMethodField()
    duration_min = serializers.IntegerField(source="service.duration_min", help_text="Duration in minutes.")
    price_from = serializers.IntegerField(help_text="The specialist's price (equal to `price_to`).")
    price_to = serializers.IntegerField(help_text="The specialist's price (equal to `price_from`).")
    specialist_ids = serializers.ListField(
        child=serializers.IntegerField(), help_text="The one specialist who offers this service."
    )

    def get_name(self, obj: selectors.ServiceOffer) -> str:
        return pick(obj.service, "name", serializer_language(self))

    def get_short_name(self, obj: selectors.ServiceOffer) -> str:
        return pick(obj.service, "short_name", serializer_language(self))

    def get_description(self, obj: selectors.ServiceOffer) -> str:
        return pick(obj.service, "description", serializer_language(self))

    def get_category_label(self, obj: selectors.ServiceOffer) -> str:
        return i18n.t(f"label.service_category.{obj.service.category}", serializer_language(self))


class ServiceListSerializer(serializers.Serializer[Any]):
    results = ServiceOfferSerializer(many=True)


__all__ = [
    "LocationSerializer",
    "ServiceListSerializer",
    "ServiceOfferSerializer",
    "SpecialistListSerializer",
    "SpecialistSerializer",
    "SpecialistServiceSerializer",
]
