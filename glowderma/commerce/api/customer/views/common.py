"""What the customer ``commerce`` views share: the fake-scenario header, the signed-in customer, the field
refusal, the cart and order responses, the own-order lookup and the stock refresh."""

from __future__ import annotations

import json
from typing import Any, cast

from django.conf import settings
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.customer_base import CustomerAPIView
from glowderma.accounts.models import Customer
from glowderma.commerce import selectors, services
from glowderma.commerce.api.customer.serializers import (
    CustomerCartSerializer,
    CustomerOrderDetailSerializer,
)
from glowderma.commerce.models import Order
from glowderma.core import integration_modes
from glowderma.core.api.exceptions import (
    field_error,
)
from glowderma.core.idempotency import already_answered, idempotency_key

FAKE_SCENARIO_HEADER = "X-GD-Fake-Scenario"


def fake_scenario_header(request: Request) -> dict[str, Any] | None:
    """``X-GD-Fake-Scenario`` (customer contract §1.2): honoured only under the local/test settings with
    ``FAKE_SCENARIO_HEADER_ENABLED`` and every provider mode fake; anything else (or malformed JSON) →
    ``None``."""
    raw = request.headers.get(FAKE_SCENARIO_HEADER)
    if not raw or not integration_modes.dev_features_allowed():
        return None
    if not getattr(settings, "FAKE_SCENARIO_HEADER_ENABLED", False):
        return None
    providers = ("devsms", "click", "payme", "uzum", "bts", "yandex", "fcm")
    if any(integration_modes.provider_mode(p) != "fake" for p in providers):
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def signed_in_customer(request: Request) -> Customer:
    return cast(Customer, request.user)


def field_refusal(name: str, code: str, **kwargs: Any) -> ValidationError:
    return ValidationError({name: [field_error(code, **kwargs)]})


def cart_response(view: CustomerAPIView, last_change: dict[str, Any] | None = None) -> Response:
    cart = selectors.cart_view(signed_in_customer(view.request).pk, view.language, last_change=last_change)
    return Response(CustomerCartSerializer(cart).data)


def order_detail_response(order: Order, lang: str, status: int = 200) -> Response:
    detail = selectors.customer_order_detail(order, lang)
    return Response(CustomerOrderDetailSerializer(detail).data, status=status)


def own_order(customer: Customer, order_id: int) -> Order:
    order = selectors.customer_order(order_id, customer.pk)
    if order is None:
        raise NotFound
    return order


def refresh_stock(request: Request, product_ids: list[int], *, key_required: bool) -> services.StockCheck:
    """Gap X-STOCK-FRESHNESS: re-read the stale lines from Billz **before** the ``@idempotent``
    transaction (``services.refresh_stale_stock``: no row lock is held during the Billz call and each read
    commits at once, so a later refusal or rollback keeps it). Live Billz only.

    Nothing is read for a request the view will refuse or replay anyway (review fix: the Billz limiter and
    circuit are shared with the sale push and the imports): a missing or malformed ``Idempotency-Key`` where
    one is required, a key whose stored response will be replayed, or a product the customer cannot see.
    The callers pass only ids of a valid body (and, for a PATCH, of a line already in the cart)."""
    if not product_ids or not services.stock_freshness_on():
        return services.FRESH
    if key_required and idempotency_key(request) is None:
        return services.FRESH
    if already_answered(request):
        return services.FRESH
    visible = selectors.visible_ids(product_ids)
    wanted = [product_id for product_id in product_ids if product_id in visible]
    return services.refresh_stale_stock(wanted) if wanted else services.FRESH


__all__ = [
    "FAKE_SCENARIO_HEADER",
    "cart_response",
    "fake_scenario_header",
    "field_refusal",
    "order_detail_response",
    "own_order",
    "refresh_stock",
    "signed_in_customer",
]
