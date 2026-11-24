"""OpenAPI declarations for ``me/sessions/*`` of the customer API (B-W1, customer contract §2.2.10): the "Faol
seanslar" list — every signed-in phone and browser of the customer — and signing one of them, or all the
others, out."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

from glowderma.core.api.schema import (
    CUSTOMER_ERRORS,
    OPTIONAL_IDEMPOTENCY_KEY_HEADER,
)
from glowderma.customers.api.customer.schemas.common import PARAMETERS, SESSION_ID_PARAM
from glowderma.customers.api.customer.serializers.sessions import (
    CustomerSessionSerializer,
    RevokedSessionsSerializer,
)

SESSION_LIST_LIST = extend_schema(
    operation_id="customer_sessions_list",
    description=(
        "Every open session of the customer (each signed-in phone or browser), most recently active "
        "first; `current` marks the caller's own."
    ),
    parameters=PARAMETERS,
    responses={200: CustomerSessionSerializer(many=True), **CUSTOMER_ERRORS},
)

SESSION_DETAIL_DELETE = extend_schema(
    operation_id="customer_sessions_destroy",
    description=(
        "Sign one session out (the row menu «Chiqish»). The caller's own session may be named too: it is "
        "a logout. Unknown, already ended or another customer's session → 404."
    ),
    parameters=[SESSION_ID_PARAM, *PARAMETERS, OPTIONAL_IDEMPOTENCY_KEY_HEADER],
    request=None,
    responses={204: None, **CUSTOMER_ERRORS},
)

REVOKE_OTHER_SESSIONS_POST = extend_schema(
    operation_id="customer_sessions_revoke_others",
    description="«Boshqa qurilmalardan chiqish»: every session but the caller's own ends.",
    parameters=[*PARAMETERS, OPTIONAL_IDEMPOTENCY_KEY_HEADER],
    request=None,
    responses={200: RevokedSessionsSerializer, **CUSTOMER_ERRORS},
)


__all__ = [
    "REVOKE_OTHER_SESSIONS_POST",
    "SESSION_DETAIL_DELETE",
    "SESSION_LIST_LIST",
]
