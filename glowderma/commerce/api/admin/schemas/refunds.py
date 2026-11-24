"""OpenAPI declarations for the refund operations of the payments screen (contract §7 item 18:
``payments/{id}/refunds/{refund_id}/ retry|resolve/``, declared here because they write order events)."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

from glowderma.commerce.api.admin.schemas.common import ACTION_ERRORS
from glowderma.commerce.api.admin.serializers import (
    ResolveRefundSerializer,
)
from glowderma.core.api.schema import (
    ADMIN_ERRORS,
)
from glowderma.payments.api.admin.serializers import PaymentSerializer

REFUND_RETRY_POST = extend_schema(
    operation_id="admin_payments_refund_retry",
    description="Retry a `failed` refund: a new refund of the payment (Click `requested`, Payme "
    "`manual_required`) and its `refund_requested` order event. 409 `click_fake_mode` (Click fake under "
    "the production settings), 409 `order_busy` (the order is busy; retry).",
    request=None,
    responses={200: PaymentSerializer, **ADMIN_ERRORS},
)

REFUND_RESOLVE_POST = extend_schema(
    operation_id="admin_payments_refund_resolve",
    description="Close an `unknown` / `manual_required` refund after a manual check, or a `failed` Click "
    "refund as `succeeded` after a fresh Click status check (`manual_payout: true` when Click still "
    "reports "
    "the payment paid and staff paid the customer by hand; 503 when Click does not answer). Audited "
    "`refund.state_changed` with the actor; order event `refund_succeeded` / `refund_failed`. 409 "
    "`click_not_reversed` (Click still shows the payment paid) / `click_no_paydoc` (no Click payment "
    "id): pay out by hand, then `manual_payout: true`; 409 `click_fake_mode` (Click fake under the "
    "production settings); 409 `order_busy` (the order is busy; retry).",
    request=ResolveRefundSerializer,
    responses={200: PaymentSerializer, **ACTION_ERRORS},
)


__all__ = [
    "REFUND_RESOLVE_POST",
    "REFUND_RETRY_POST",
]
