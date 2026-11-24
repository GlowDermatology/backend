"""OpenAPI declarations for the storefront landing page (``GET home/``, **O**)."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

from glowderma.core.api.schema import (
    PUBLIC_CUSTOMER_ERRORS,
)
from glowderma.customers.api.customer import serializers as s
from glowderma.customers.api.customer.schemas.common import PUBLIC_PARAMETERS

HOME_GET = extend_schema(
    operation_id="customer_home_retrieve",
    description=(
        "The storefront landing page: the shop numbers, the spotlight, the product rails (`ProductCard`) and "
        "the active cashback tiers. Public cache 60 s."
    ),
    parameters=PUBLIC_PARAMETERS,
    responses={200: s.HomeSerializer, **PUBLIC_CUSTOMER_ERRORS},
)


__all__ = [
    "HOME_GET",
]
