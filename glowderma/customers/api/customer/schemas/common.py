"""OpenAPI declarations shared by the customer views: the header parameter lists, the path parameters and the
polymorphic sign-in answer."""

from __future__ import annotations

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, PolymorphicProxySerializer

from glowderma.core.api.schema import (
    ACCEPT_LANGUAGE_HEADER,
    DEVICE_HEADERS,
    IDEMPOTENCY_KEY_HEADER,
    OPTIONAL_DEVICE_HEADERS,
    OPTIONAL_IDEMPOTENCY_KEY_HEADER,
)
from glowderma.customers.api.customer import serializers as s
from glowderma.integrations.oauth.registry import CUSTOMER_PROVIDERS

# ``auth/login/`` and ``auth/social/`` answer either AuthTokens or a NextStep (customer contract §2.0).
AUTH_TOKENS_OR_NEXT_STEP = PolymorphicProxySerializer(
    component_name="AuthTokensOrNextStep",
    serializers=[s.AuthTokensSerializer, s.NextStepSerializer],
    resource_type_field_name=None,
)


PARAMETERS = [*DEVICE_HEADERS, ACCEPT_LANGUAGE_HEADER]


I_PARAMETERS = [*PARAMETERS, IDEMPOTENCY_KEY_HEADER]


O_PARAMETERS = [*PARAMETERS, OPTIONAL_IDEMPOTENCY_KEY_HEADER]


PROVIDER_PARAM = OpenApiParameter(
    name="provider", type=OpenApiTypes.STR, location=OpenApiParameter.PATH, enum=list(CUSTOMER_PROVIDERS)
)


ADDRESS_ID_PARAM = OpenApiParameter(name="id", type=OpenApiTypes.INT, location=OpenApiParameter.PATH)


SESSION_ID_PARAM = OpenApiParameter(name="id", type=OpenApiTypes.UUID, location=OpenApiParameter.PATH)


PUBLIC_PARAMETERS = [*OPTIONAL_DEVICE_HEADERS, ACCEPT_LANGUAGE_HEADER]


__all__ = [
    "ADDRESS_ID_PARAM",
    "AUTH_TOKENS_OR_NEXT_STEP",
    "I_PARAMETERS",
    "O_PARAMETERS",
    "PARAMETERS",
    "PROVIDER_PARAM",
    "PUBLIC_PARAMETERS",
    "SESSION_ID_PARAM",
]
