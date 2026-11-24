"""OpenAPI declarations for the admin customer list (P E) and card (admin contract §2.6)."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

from glowderma.core.api.schema import (
    ADMIN_ERRORS,
    EXPORT_PARAM,
)
from glowderma.customers.api.admin.serializers import (
    CustomerCardSerializer,
    CustomerListQuerySerializer,
)
from glowderma.notifications.api.admin.serializers import CustomerListItemSerializer

CUSTOMER_LIST = extend_schema(
    operation_id="admin_customers_list",
    description=(
        "`search`: the name contains the term, or (≥ 3 digits) the phone contains its digits, or "
        "(≥ 3 characters) the e-mail address contains it. `segment`: segment membership (rules or "
        "manual, active customers only). Orderings tie by `id`. Accounts without a phone yet show "
        "`phone: null` and their `email` (exports put the e-mail in the phone column)."
    ),
    parameters=[CustomerListQuerySerializer, EXPORT_PARAM],
    responses={200: CustomerListItemSerializer(many=True), **ADMIN_ERRORS},
)

CUSTOMER_RETRIEVE = extend_schema(
    operation_id="admin_customers_retrieve",
    description='The customer card of an active or deleted customer (deleted: name `""`, `deleted_at`).',
    responses={200: CustomerCardSerializer, **ADMIN_ERRORS},
)


__all__ = [
    "CUSTOMER_LIST",
    "CUSTOMER_RETRIEVE",
]
