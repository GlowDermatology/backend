"""``GET delivery/regions/`` (customer contract §2.6, **O**): BTS's region/city reference list for the address
form, cached 24 h; empty with ``updated_at`` null while delivery is not offered."""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.customer_base import PublicAPIView
from glowderma.core.api.exceptions import ProviderUnavailable
from glowderma.core.api.schema import (
    OPTIONAL_DEVICE_HEADERS,
    PUBLIC_CUSTOMER_ERRORS,
    CustomerErrorEnvelopeSerializer,
)
from glowderma.delivery import selectors
from glowderma.integrations.bts.ports import BtsUnavailable


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


class DeliveryRegionsView(PublicAPIView):
    public_cache_seconds = 60

    @extend_schema(
        operation_id="customer_delivery_regions",
        parameters=OPTIONAL_DEVICE_HEADERS,
        responses={
            200: RegionCatalogSerializer,
            **PUBLIC_CUSTOMER_ERRORS,
            503: CustomerErrorEnvelopeSerializer,
        },
    )
    def get(self, request: Request) -> Response:
        try:
            catalog = selectors.region_catalog()
        except BtsUnavailable as exc:
            raise ProviderUnavailable() from exc
        return Response(RegionCatalogSerializer(catalog).data)
