"""OpenAPI declarations for ``me/addresses/*`` of the customer API (customer contract §2.2.7); another
customer's address id answers 404."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

from glowderma.core.api.schema import (
    CUSTOMER_ERRORS,
)
from glowderma.customers.api.customer import serializers as s
from glowderma.customers.api.customer.schemas.common import (
    ADDRESS_ID_PARAM,
    I_PARAMETERS,
    O_PARAMETERS,
    PARAMETERS,
)

ADDRESS_LIST_GET = extend_schema(
    operation_id="customer_me_addresses_list",
    parameters=PARAMETERS,
    responses={200: s.CustomerAddressListSerializer, **CUSTOMER_ERRORS},
)

ADDRESS_LIST_POST = extend_schema(
    operation_id="customer_me_addresses_create",
    parameters=I_PARAMETERS,
    request=s.AddressWriteSerializer,
    responses={201: s.CustomerAddressSerializer, **CUSTOMER_ERRORS},
)

ADDRESS_DETAIL_PATCH = extend_schema(
    operation_id="customer_me_addresses_partial_update",
    parameters=[*O_PARAMETERS, ADDRESS_ID_PARAM],
    request=s.AddressWriteSerializer,
    responses={200: s.CustomerAddressSerializer, **CUSTOMER_ERRORS},
)

ADDRESS_DETAIL_DELETE = extend_schema(
    operation_id="customer_me_addresses_destroy",
    parameters=[*O_PARAMETERS, ADDRESS_ID_PARAM],
    responses={204: None, **CUSTOMER_ERRORS},
)

ADDRESS_DEFAULT_POST = extend_schema(
    operation_id="customer_me_addresses_default",
    parameters=[*O_PARAMETERS, ADDRESS_ID_PARAM],
    request=None,
    responses={200: s.CustomerAddressSerializer, **CUSTOMER_ERRORS},
)


__all__ = [
    "ADDRESS_DEFAULT_POST",
    "ADDRESS_DETAIL_DELETE",
    "ADDRESS_DETAIL_PATCH",
    "ADDRESS_LIST_GET",
    "ADDRESS_LIST_POST",
]
