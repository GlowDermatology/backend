"""The server cart (customer contract §2.5): read, add, change and remove items."""

from __future__ import annotations

from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.customer_base import CustomerAPIView
from glowderma.commerce import selectors, services
from glowderma.commerce.api.customer.schemas.cart import (
    CART_DELETE,
    CART_GET,
    CART_ITEM_DELETE,
    CART_ITEM_PATCH,
    CART_ITEMS_DELETE,
    CART_ITEMS_POST,
)
from glowderma.commerce.api.customer.serializers import (
    CustomerCartAddSerializer,
    CustomerCartQuantitySerializer,
)
from glowderma.commerce.api.customer.views.common import (
    cart_response,
    field_refusal,
    refresh_stock,
    signed_in_customer,
)
from glowderma.core import integration_modes
from glowderma.core.api.exceptions import (
    OnlineSalesUnavailable,
)
from glowderma.core.idempotency import idempotent

BULK_DELETE_MAX = 50


class CartView(CustomerAPIView):
    @CART_GET
    def get(self, request: Request) -> Response:
        return cart_response(self)

    @CART_DELETE
    @idempotent(required=False)
    def delete(self, request: Request) -> Response:
        services.clear_cart(signed_in_customer(request).pk)
        return cart_response(self)


class CartItemsView(CustomerAPIView):
    @CART_ITEMS_POST
    def post(self, request: Request) -> Response:
        # stale stock re-read from Billz (§2.5); never refuses; nothing read for a body that cannot pass
        body = CustomerCartAddSerializer(data=request.data)
        if services.stock_freshness_on() and body.is_valid():
            refresh_stock(request, [body.validated_data["product_id"]], key_required=True)
        return self._add(request)

    @idempotent()
    def _add(self, request: Request) -> Response:
        if not integration_modes.online_sales_enabled():
            raise OnlineSalesUnavailable()
        body = CustomerCartAddSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        try:
            change = services.add_to_cart(
                signed_in_customer(request).pk,
                body.validated_data["product_id"],
                body.validated_data["quantity"],
            )
        except services.ProductNotFound as exc:
            raise field_refusal("product_id", "does_not_exist") from exc
        return cart_response(self, change)

    @CART_ITEMS_DELETE
    @idempotent(required=False)
    def delete(self, request: Request) -> Response:
        raw = request.query_params.getlist("product_id")
        if not raw:
            raise field_refusal("product_id", "required")
        if len(raw) > BULK_DELETE_MAX:
            raise field_refusal("product_id", "max_items", limit=BULK_DELETE_MAX)
        try:
            ids = [int(value) for value in raw]
        except ValueError as exc:
            raise field_refusal("product_id", "invalid") from exc
        services.remove_from_cart(signed_in_customer(request).pk, ids)
        return cart_response(self)


class CartItemView(CustomerAPIView):
    @CART_ITEM_PATCH
    def patch(self, request: Request, product_id: int) -> Response:
        # stale stock re-read from Billz (§2.5); never refuses; only a line already in the cart, valid body
        if (
            services.stock_freshness_on()
            and CustomerCartQuantitySerializer(data=request.data).is_valid()
            and product_id in selectors.cart_product_ids(signed_in_customer(request).pk)
        ):
            refresh_stock(request, [product_id], key_required=False)
        return self._set_quantity(request, product_id)

    @idempotent(required=False)
    def _set_quantity(self, request: Request, product_id: int) -> Response:
        if not integration_modes.online_sales_enabled():
            raise OnlineSalesUnavailable()
        body = CustomerCartQuantitySerializer(data=request.data)
        body.is_valid(raise_exception=True)
        try:
            change = services.set_cart_quantity(
                signed_in_customer(request).pk, product_id, body.validated_data["quantity"]
            )
        except (services.CartLineNotFound, services.ProductNotFound) as exc:
            raise NotFound from exc
        return cart_response(self, change)

    @CART_ITEM_DELETE
    @idempotent(required=False)
    def delete(self, request: Request, product_id: int) -> Response:
        services.remove_from_cart(signed_in_customer(request).pk, [product_id])
        return cart_response(self)


__all__ = [
    "BULK_DELETE_MAX",
    "CartItemView",
    "CartItemsView",
    "CartView",
]
