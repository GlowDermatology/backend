"""OpenAPI declarations for the admin order actions (contract §2.2): cancel, transition, retry the shipment
and confirm a return."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

from glowderma.commerce.api.admin.schemas.common import ACTION_ERRORS
from glowderma.commerce.api.admin.serializers import (
    CancelOrderSerializer,
    ConfirmReturnSerializer,
    OrderDetailSerializer,
    TransitionSerializer,
)
from glowderma.core.api.schema import (
    ADMIN_ERRORS,
    IDEMPOTENCY_KEY_HEADER,
)

ORDER_CANCEL_POST = extend_schema(
    operation_id="admin_orders_cancel",
    description="Cancel an order (full refund when paid). A Yandex Delivery order is first made "
    "carrier-safe outside the transaction (plan §3.8): an open courier claim is cancelled at Yandex "
    "before pickup (free or paid), after pickup a return to the warehouse is requested and the cancel "
    "proceeds once Yandex reports it `returning`; a returning / returned parcel needs no call, a "
    "delivered order is refused. 409 `not_cancellable` (Yandex refused, delivered, parcel not coming "
    "back), 409 `return_requested` (return asked, not confirmed yet: retry later; the order is "
    "unchanged), 409 `conflict` (another Yandex operation of the order is running, or Yandex has not "
    "shown the cancel yet), 503 `provider_unavailable` (Yandex did not answer; nothing changed).",
    parameters=[IDEMPOTENCY_KEY_HEADER],
    request=CancelOrderSerializer,
    responses={200: OrderDetailSerializer, **ACTION_ERRORS},
)

ORDER_TRANSITION_POST = extend_schema(
    operation_id="admin_orders_transition",
    parameters=[IDEMPOTENCY_KEY_HEADER],
    request=TransitionSerializer,
    responses={200: OrderDetailSerializer, **ADMIN_ERRORS},
)

ORDER_RETRY_SHIPMENT_POST = extend_schema(
    operation_id="admin_orders_retry_shipment",
    parameters=[IDEMPOTENCY_KEY_HEADER],
    request=None,
    responses={202: OrderDetailSerializer, **ADMIN_ERRORS},
)

ORDER_CONFIRM_RETURN_POST = extend_schema(
    operation_id="admin_orders_confirm_return",
    description="Confirm that the parcel of a Yandex order whose delivery stage is `returning` / "
    "`returned` is physically back at the warehouse: stage `returned`, `returned_at`, the open "
    "`return_pending` issue resolved (the note is its resolution note), order event "
    "`shipment_returned`, audit `order.return_confirmed`. A cancelled order stays cancelled; an open "
    "order stays as it is (staff dispatch again or cancel). 409 `invalid_transition` (no returning "
    "parcel), 409 `conflict` (confirmed already), 409 `order_busy` (the order is busy).",
    parameters=[IDEMPOTENCY_KEY_HEADER],
    request=ConfirmReturnSerializer,
    responses={200: OrderDetailSerializer, **ADMIN_ERRORS},
)


__all__ = [
    "ORDER_CANCEL_POST",
    "ORDER_CONFIRM_RETURN_POST",
    "ORDER_RETRY_SHIPMENT_POST",
    "ORDER_TRANSITION_POST",
]
