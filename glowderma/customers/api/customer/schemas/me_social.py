"""OpenAPI declarations for ``me/social/*`` of the customer API (customer contract §2.2.8): linking and
unlinking a social identity."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

from glowderma.core.api.schema import (
    CUSTOMER_ERRORS,
)
from glowderma.customers.api.customer import serializers as s
from glowderma.customers.api.customer.schemas.common import I_PARAMETERS, O_PARAMETERS, PROVIDER_PARAM

SOCIAL_LINK_POST = extend_schema(
    operation_id="customer_me_social_link",
    parameters=I_PARAMETERS,
    request=s.MeSocialLinkSerializer,
    responses={200: s.CustomerMeSerializer, **CUSTOMER_ERRORS},
)

SOCIAL_UNLINK_DELETE = extend_schema(
    operation_id="customer_me_social_unlink",
    parameters=[*O_PARAMETERS, PROVIDER_PARAM],
    request=s.ReauthSerializer,
    responses={200: s.CustomerMeSerializer, **CUSTOMER_ERRORS},
)


__all__ = [
    "SOCIAL_LINK_POST",
    "SOCIAL_UNLINK_DELETE",
]
