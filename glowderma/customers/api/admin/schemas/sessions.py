"""OpenAPI declarations for admin view of a customer's signed-in devices (B-W1, admin contract §2.6): the same
session list the customer sees under «Faol seanslar», and signing one session out (support: a lost phone, a
shared computer)."""

from __future__ import annotations

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema

from glowderma.core.api.schema import (
    ADMIN_ERRORS,
    OPTIONAL_IDEMPOTENCY_KEY_HEADER,
)
from glowderma.customers.api.admin.serializers.sessions import AdminCustomerSessionSerializer

SESSION_ID_PARAM = OpenApiParameter(name="session_id", type=OpenApiTypes.UUID, location=OpenApiParameter.PATH)

CUSTOMER_SESSIONS_LIST = extend_schema(
    operation_id="admin_customers_sessions_list",
    description=("The customer's open sessions (signed-in phones and browsers), most recently active first."),
    responses={200: AdminCustomerSessionSerializer(many=True), **ADMIN_ERRORS},
)

CUSTOMER_SESSION_DETAIL_DELETE = extend_schema(
    operation_id="admin_customers_sessions_destroy",
    description=(
        "Sign one session of the customer out (reason `admin_revoked`; the device stops receiving pushes "
        "when no other session is left on it). Unknown or already ended session → 404."
    ),
    parameters=[SESSION_ID_PARAM, OPTIONAL_IDEMPOTENCY_KEY_HEADER],
    request=None,
    responses={204: None, **ADMIN_ERRORS},
)


__all__ = [
    "CUSTOMER_SESSIONS_LIST",
    "CUSTOMER_SESSION_DETAIL_DELETE",
    "SESSION_ID_PARAM",
]
