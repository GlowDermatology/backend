"""OpenAPI declarations for the admin cashback adjustment of a customer (admin contract §2.6, I)."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

from glowderma.core.api.schema import (
    ADMIN_ERRORS,
    IDEMPOTENCY_KEY_HEADER,
)
from glowderma.customers.api.admin.serializers import (
    CashbackAdjustmentResultSerializer,
    CashbackAdjustmentSerializer,
)

CASHBACK_ADJUSTMENT_POST = extend_schema(
    operation_id="admin_customers_cashback_adjustments_create",
    description=(
        'Checks in order: `amount` 1..10 000 000 ("Summani kiriting."), `reason` trimmed 3..300 '
        '("Sababni kiriting."), then for `subtract` `amount ≤ available` under the account lock '
        '("Balans manfiy bo‘lib qoladi."). Allowed for deleted customers.'
    ),
    parameters=[IDEMPOTENCY_KEY_HEADER],
    request=CashbackAdjustmentSerializer,
    responses={201: CashbackAdjustmentResultSerializer, **ADMIN_ERRORS},
)


__all__ = [
    "CASHBACK_ADJUSTMENT_POST",
]
