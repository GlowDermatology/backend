"""``GET delivery/regions/`` (customer contract §2.6, **O**): BTS's region/city reference list for the address
form, cached 24 h; empty with ``updated_at`` null while delivery is not offered."""

from __future__ import annotations

from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.customer_base import PublicAPIView
from glowderma.core.api.exceptions import ProviderUnavailable
from glowderma.delivery import selectors
from glowderma.delivery.api.customer.schemas import DELIVERY_REGIONS_GET
from glowderma.delivery.api.customer.serializers import (
    RegionCatalogSerializer,
)
from glowderma.integrations.bts.ports import BtsUnavailable


class DeliveryRegionsView(PublicAPIView):
    public_cache_seconds = 60

    @DELIVERY_REGIONS_GET
    def get(self, request: Request) -> Response:
        try:
            catalog = selectors.region_catalog()
        except BtsUnavailable as exc:
            raise ProviderUnavailable() from exc
        return Response(RegionCatalogSerializer(catalog).data)
