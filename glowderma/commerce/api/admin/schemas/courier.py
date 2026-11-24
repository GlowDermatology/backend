"""OpenAPI declarations for dispatching an order to a courier (contract §2.2)."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

from glowderma.commerce.api.admin.serializers import (
    DispatchSerializer,
    OrderDetailSerializer,
)
from glowderma.core.api.schema import (
    ADMIN_ERRORS,
    IDEMPOTENCY_KEY_HEADER,
)

ORDER_DISPATCH_POST = extend_schema(
    operation_id="admin_orders_dispatch",
    description="Call a Yandex courier for a paid delivery order (plan §3.6): staff confirm they checked "
    "the contents and give the parcel weight; `approved_price` accepts the offer of an open price review "
    "(`dispatch_review`), `method` switches Standard / Express. The parcel and a new courier claim are "
    "stored and the dispatch is queued — nothing is sent to Yandex in the request (202, "
    "`delivery.stage` = `courier_requested`); the order's open `dispatch_failed` / `dispatch_review` / "
    "`preparation_overdue` issues are resolved. 409 `invalid_transition` (not a paid, open Yandex "
    "delivery waiting for a courier: stage `awaiting_dispatch` / `failed` / `returned`, a "
    "`handed_to_courier` order only once its parcel is `returned`), 409 `conflict` (a courier claim is "
    "still open — a dispatch is running or a courier may come —, a cancel is in progress, "
    "`approved_price` without an open price review, or the order is busy), 409 `coordinates_required` "
    "(the address has no map pin), 409 `method_disabled` (the method switched to is turned off), 409 "
    "`too_heavy` (the weight is above the method's class limit: courier 10 kg, express 20 kg), 409 "
    "`not_configured` (the warehouse data is incomplete), 409 `suspended` (dispatch is suspended), 409 "
    "`closed` (the warehouse is closed now), 409 `after_cutoff` (past today's dispatch deadline), 409 "
    "`returns_unavailable` (the warehouse does not take returned parcels now).",
    parameters=[IDEMPOTENCY_KEY_HEADER],
    request=DispatchSerializer,
    responses={202: OrderDetailSerializer, **ADMIN_ERRORS},
)


__all__ = [
    "ORDER_DISPATCH_POST",
]
