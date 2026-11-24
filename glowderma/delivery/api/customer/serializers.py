"""The shapes of delivery/regions/: a city, a region and the catalog."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers


class CitySerializer(serializers.Serializer[Any]):
    code = serializers.CharField()
    name = serializers.CharField()


class RegionSerializer(serializers.Serializer[Any]):
    code = serializers.CharField()
    name = serializers.CharField()
    cities = CitySerializer(many=True)


class RegionCatalogSerializer(serializers.Serializer[Any]):
    regions = RegionSerializer(many=True)
    updated_at = serializers.DateTimeField(allow_null=True)
