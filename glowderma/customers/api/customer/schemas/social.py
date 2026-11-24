"""OpenAPI declarations for ``auth/social/*`` of the customer API (customer contract §2.1.8): social sign-in
with Google, Facebook and Apple, and its phone completion."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

from glowderma.core.api.schema import (
    CUSTOMER_ERRORS,
)
from glowderma.customers.api.customer import serializers as s
from glowderma.customers.api.customer.schemas.common import AUTH_TOKENS_OR_NEXT_STEP, PARAMETERS

SOCIAL_NONCE_GET = extend_schema(
    operation_id="customer_auth_social_nonce",
    auth=[],
    parameters=PARAMETERS,
    responses={200: s.NonceSerializer, **CUSTOMER_ERRORS},
)

SOCIAL_POST = extend_schema(
    operation_id="customer_auth_social",
    auth=[],
    parameters=PARAMETERS,
    request=s.SocialSerializer,
    responses={200: AUTH_TOKENS_OR_NEXT_STEP, **CUSTOMER_ERRORS},
)

SOCIAL_COMPLETE_POST = extend_schema(
    operation_id="customer_auth_social_complete",
    auth=[],
    parameters=PARAMETERS,
    request=s.SocialCompleteSerializer,
    responses={200: s.AuthTokensSerializer, 201: s.AuthTokensSerializer, **CUSTOMER_ERRORS},
)


__all__ = [
    "SOCIAL_COMPLETE_POST",
    "SOCIAL_NONCE_GET",
    "SOCIAL_POST",
]
