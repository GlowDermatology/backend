"""What the admin ``commerce`` views share: the order lookup, the actor and the fresh order detail."""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.commerce import selectors, services
from glowderma.commerce.api.admin.serializers import (
    OrderDetailSerializer,
)
from glowderma.core.api.exceptions import Conflict
from glowderma.loyalty.services import PurchaseConflict


def order_or_404[T](call: Callable[[], T]) -> T:
    """Run a service call, mapping a missing order to 404 and a loyalty purchase conflict to 409."""
    try:
        return call()
    except services.OrderNotFound as exc:
        raise NotFound from exc
    except PurchaseConflict as exc:  # loyalty's text is internal English: answer with the Uzbek message
        raise Conflict(services.MESSAGE_PURCHASE_CONFLICT) from exc


def actor_id(request: Request) -> int:
    return cast(int, request.user.pk)


def order_detail_response(order_id: int, status: int = 200) -> Response:
    detail = selectors.order_detail(order_id)
    if detail is None:
        raise NotFound
    return Response(OrderDetailSerializer(detail).data, status=status)


__all__ = [
    "actor_id",
    "order_detail_response",
    "order_or_404",
]
