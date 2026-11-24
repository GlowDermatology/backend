"""OpenAPI declarations for ``GET delivery/regions/`` (customer contract §2.6, **O**): BTS's region/city
reference list for the address form, cached 24 h; empty with ``updated_at`` null while delivery is not
offered."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

from glowderma.core.api.schema import (
    OPTIONAL_DEVICE_HEADERS,
    PUBLIC_CUSTOMER_ERRORS,
    CustomerErrorEnvelopeSerializer,
)
from glowderma.delivery.api.customer.serializers import (
    RegionCatalogSerializer,
)

DELIVERY_REGIONS_GET = extend_schema(
    operation_id="customer_delivery_regions",
    parameters=OPTIONAL_DEVICE_HEADERS,
    responses={
        200: RegionCatalogSerializer,
        **PUBLIC_CUSTOMER_ERRORS,
        503: CustomerErrorEnvelopeSerializer,
    },
)


__all__ = [
    "DELIVERY_REGIONS_GET",
]
