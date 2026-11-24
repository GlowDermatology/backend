"""The translated write shapes (Uzbek required on create where the model requires it)."""

from __future__ import annotations

from rest_framework import serializers


class SpecialistTextSerializer(serializers.Serializer[dict[str, str]]):
    """``qualifications`` / ``bio``: ``{uz, ru, en}``, each ≤ 2000, every language optional (missing on
    create → ``""``; a PATCH changes only the languages sent)."""

    uz = serializers.CharField(max_length=2000, allow_blank=True, required=False, default="")
    ru = serializers.CharField(max_length=2000, allow_blank=True, required=False, default="")
    en = serializers.CharField(max_length=2000, allow_blank=True, required=False, default="")


class ServiceNameSerializer(serializers.Serializer[dict[str, str]]):
    """``name``: ``uz`` trimmed 2..120 required on create; ``ru`` / ``en`` ≤ 120 optional."""

    uz = serializers.CharField(min_length=2, max_length=120)
    ru = serializers.CharField(max_length=120, allow_blank=True, required=False, default="")
    en = serializers.CharField(max_length=120, allow_blank=True, required=False, default="")


class ServiceShortNameSerializer(serializers.Serializer[dict[str, str]]):
    """``short_name``: ``uz`` 1..40 (defaults to the first 40 characters of ``name.uz`` on create); ``ru`` /
    ``en`` ≤ 40 optional."""

    uz = serializers.CharField(max_length=40)
    ru = serializers.CharField(max_length=40, allow_blank=True, required=False, default="")
    en = serializers.CharField(max_length=40, allow_blank=True, required=False, default="")


class ServiceDescriptionSerializer(serializers.Serializer[dict[str, str]]):
    """``description``: ``{uz, ru, en}``, each ≤ 2000, optional."""

    uz = serializers.CharField(max_length=2000, allow_blank=True, required=False, default="")
    ru = serializers.CharField(max_length=2000, allow_blank=True, required=False, default="")
    en = serializers.CharField(max_length=2000, allow_blank=True, required=False, default="")


class LocationNameSerializer(serializers.Serializer[dict[str, str]]):
    """Translated clinic display name."""

    uz = serializers.CharField(min_length=2, max_length=120, required=False)
    ru = serializers.CharField(max_length=120, allow_blank=True, required=False)
    en = serializers.CharField(max_length=120, allow_blank=True, required=False)


class LocationAddressSerializer(serializers.Serializer[dict[str, str]]):
    """Translated street address."""

    uz = serializers.CharField(max_length=300, required=False)
    ru = serializers.CharField(max_length=300, allow_blank=True, required=False)
    en = serializers.CharField(max_length=300, allow_blank=True, required=False)


class LocationDirectionsSerializer(serializers.Serializer[dict[str, str]]):
    uz = serializers.CharField(max_length=500, allow_blank=True, required=False)
    ru = serializers.CharField(max_length=500, allow_blank=True, required=False)
    en = serializers.CharField(max_length=500, allow_blank=True, required=False)


class LocationCreateNameSerializer(LocationNameSerializer):
    uz = serializers.CharField(min_length=2, max_length=120)


class LocationCreateAddressSerializer(LocationAddressSerializer):
    uz = serializers.CharField(max_length=300)


__all__ = [
    "LocationAddressSerializer",
    "LocationCreateAddressSerializer",
    "LocationCreateNameSerializer",
    "LocationDirectionsSerializer",
    "LocationNameSerializer",
    "ServiceDescriptionSerializer",
    "ServiceNameSerializer",
    "ServiceShortNameSerializer",
    "SpecialistTextSerializer",
]
