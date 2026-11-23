"""OpenAPI declarations shared by the staff admin views: the error maps, the provider parameter and the
polymorphic sign-in result."""

from __future__ import annotations

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, PolymorphicProxySerializer

from glowderma.accounts.api.admin.serializers import (
    LoginResponseSerializer,
    MfaChallengeSerializer,
)
from glowderma.core.api.schema import (
    ErrorEnvelopeSerializer,
)
from glowderma.integrations.oauth.registry import PROVIDER_NAMES

ERRORS = {400: ErrorEnvelopeSerializer, 401: ErrorEnvelopeSerializer, 429: ErrorEnvelopeSerializer}


PROVIDER_PARAM = OpenApiParameter(
    name="provider", type=OpenApiTypes.STR, location=OpenApiParameter.PATH, enum=list(PROVIDER_NAMES)
)


# The 200 of a sign-in: a session, or (admin panel MFA on) the challenge for auth/mfa/verify/.
LOGIN_RESULT = PolymorphicProxySerializer(
    component_name="AdminLoginResult",
    serializers=[LoginResponseSerializer, MfaChallengeSerializer],
    resource_type_field_name=None,
)


__all__ = [
    "ERRORS",
    "LOGIN_RESULT",
    "PROVIDER_PARAM",
]
