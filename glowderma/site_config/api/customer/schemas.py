"""OpenAPI declarations for ``GET settings/public/`` (customer contract §2.4, **O**, cached 60 s)."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

from glowderma.core.api.schema import ACCEPT_LANGUAGE_HEADER, OPTIONAL_DEVICE_HEADERS, PUBLIC_CUSTOMER_ERRORS
from glowderma.site_config.api.customer.serializers import (
    PublicSettingsSerializer,
)

PUBLIC_SETTINGS_GET = extend_schema(
    operation_id="customer_settings_public_retrieve",
    parameters=[*OPTIONAL_DEVICE_HEADERS, ACCEPT_LANGUAGE_HEADER],
    responses={200: PublicSettingsSerializer, **PUBLIC_CUSTOMER_ERRORS},
)


__all__ = [
    "PUBLIC_SETTINGS_GET",
]
