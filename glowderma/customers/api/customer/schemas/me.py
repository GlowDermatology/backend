"""OpenAPI declarations for ``me/`` of the customer API (customer contract §2.2): profile, deletion, consent,
image, device and password. Every query starts from the signed-in customer."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

from glowderma.core.api.schema import (
    CUSTOMER_ERRORS,
)
from glowderma.customers.api.customer import serializers as s
from glowderma.customers.api.customer.schemas.common import I_PARAMETERS, O_PARAMETERS, PARAMETERS

ME_GET = extend_schema(
    operation_id="customer_me_retrieve",
    parameters=PARAMETERS,
    responses={200: s.CustomerMeSerializer, **CUSTOMER_ERRORS},
)

ME_PATCH = extend_schema(
    operation_id="customer_me_partial_update",
    parameters=O_PARAMETERS,
    request=s.MePatchSerializer,
    responses={200: s.CustomerMeSerializer, **CUSTOMER_ERRORS},
)

ME_DELETE = extend_schema(
    operation_id="customer_me_destroy",
    parameters=O_PARAMETERS,
    request=s.ReauthSerializer,
    responses={204: None, **CUSTOMER_ERRORS},
)

CONSENT_POST = extend_schema(
    operation_id="customer_me_consent",
    parameters=O_PARAMETERS,
    request=s.ConsentSerializer,
    responses={200: s.CustomerMeSerializer, **CUSTOMER_ERRORS},
)

IMAGE_PUT = extend_schema(
    operation_id="customer_me_image_update",
    parameters=O_PARAMETERS,
    request={
        "multipart/form-data": {
            "type": "object",
            "properties": {"file": {"type": "string", "format": "binary"}},
        }
    },
    responses={200: s.ImageUrlSerializer, **CUSTOMER_ERRORS},
)

IMAGE_DELETE = extend_schema(
    operation_id="customer_me_image_destroy",
    parameters=O_PARAMETERS,
    responses={204: None, **CUSTOMER_ERRORS},
)

DEVICE_PUT = extend_schema(
    operation_id="customer_me_device_update",
    parameters=O_PARAMETERS,
    request=s.DeviceSerializer,
    responses={200: s.DeviceStateSerializer, **CUSTOMER_ERRORS},
)

PASSWORD_POST = extend_schema(
    operation_id="customer_me_password",
    parameters=I_PARAMETERS,
    request=s.MePasswordSerializer,
    responses={200: s.CustomerMeSerializer, **CUSTOMER_ERRORS},
)


__all__ = [
    "CONSENT_POST",
    "DEVICE_PUT",
    "IMAGE_DELETE",
    "IMAGE_PUT",
    "ME_DELETE",
    "ME_GET",
    "ME_PATCH",
    "PASSWORD_POST",
]
