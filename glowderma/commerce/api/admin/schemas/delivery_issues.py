"""OpenAPI declarations for the delivery exception queue (Yandex Delivery plan §3.9): list, open on an order
and resolve."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

from glowderma.commerce.api.admin.serializers import (
    DeliveryIssueQuerySerializer,
    DeliveryIssueSerializer,
    OpenIssueSerializer,
    ResolveIssueSerializer,
)
from glowderma.core.api.schema import (
    ADMIN_ERRORS,
    IDEMPOTENCY_KEY_HEADER,
)

DELIVERY_ISSUE_LIST = extend_schema(
    operation_id="admin_delivery_issues_list",
    description="The delivery exception queue: open issues first (urgent first, then oldest first); "
    "`state=resolved` lists the newest resolved first.",
    parameters=[DeliveryIssueQuerySerializer],
    responses={200: DeliveryIssueSerializer(many=True), **ADMIN_ERRORS},
)

ORDER_ISSUE_CREATE_POST = extend_schema(
    operation_id="admin_orders_issues_create",
    description="Staff report a delivery incident of a delivery order (a wrong or missing product…): "
    "`kind` must be `incident`. 201 opens the order's incident issue; 200 refreshes the one already "
    "open (note and urgency). Audited `delivery.issue_opened`; an urgent incident alerts staff. A pickup "
    "order → 409 `conflict`.",
    parameters=[IDEMPOTENCY_KEY_HEADER],
    request=OpenIssueSerializer,
    responses={201: DeliveryIssueSerializer, 200: DeliveryIssueSerializer, **ADMIN_ERRORS},
)

DELIVERY_ISSUE_RESOLVE_POST = extend_schema(
    operation_id="admin_delivery_issues_resolve",
    description="Close an open issue with a note (audited `delivery.issue_resolved`); 409 `conflict` "
    "when it is resolved already.",
    parameters=[IDEMPOTENCY_KEY_HEADER],
    request=ResolveIssueSerializer,
    responses={200: DeliveryIssueSerializer, **ADMIN_ERRORS},
)


__all__ = [
    "DELIVERY_ISSUE_LIST",
    "DELIVERY_ISSUE_RESOLVE_POST",
    "ORDER_ISSUE_CREATE_POST",
]
