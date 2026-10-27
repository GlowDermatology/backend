"""Customer routes of `commerce` (customer contract §2.5–§2.8): the server cart, ``delivery/calculate/``,
checkout, pay, customer cancel, reorder and the customer's orders. ``delivery/calculate/`` lives here (it
reads the cart, ``loyalty`` and ``commerce.selectors.cashback_max``, which ``delivery`` may not import);
``delivery`` serves ``delivery/regions/``."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, cast
from urllib.parse import urlencode

from django.conf import settings
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts import services as account_services
from glowderma.accounts.api.customer_base import CustomerAPIView, CustomerGenericViewSet
from glowderma.accounts.api.throttles import CheckoutThrottle, CustomerRateThrottle, PayThrottle
from glowderma.accounts.models import Address, AuthSession, Customer
from glowderma.commerce import payment_ref, selectors, services, state
from glowderma.commerce.api.customer.serializers import (
    CustomerCartAddSerializer,
    CustomerCartQuantitySerializer,
    CustomerCartSerializer,
    CustomerCheckoutSerializer,
    CustomerDeliveryCalculateSerializer,
    CustomerDeliveryQuoteSerializer,
    CustomerOrderDetailSerializer,
    CustomerOrderScopeQuerySerializer,
    CustomerOrderSummarySerializer,
    CustomerPayLinkSerializer,
    CustomerPaySerializer,
)
from glowderma.commerce.models import Order
from glowderma.core import enums, integration_modes, timeutils
from glowderma.core.api.exceptions import (
    AlreadyPaid,
    CartEmpty,
    DeliveryUnavailable,
    NotPayable,
    OnlineSalesUnavailable,
    OrderExpired,
    PaymentInProgress,
    PriceChanged,
    ProviderUnavailable,
    TermsUpdateRequired,
    field_error,
)
from glowderma.core.api.exceptions import OutOfStock as OutOfStockError
from glowderma.core.api.schema import (
    CUSTOMER_ERRORS,
    DEVICE_HEADERS,
    IDEMPOTENCY_KEY_HEADER,
    OPTIONAL_IDEMPOTENCY_KEY_HEADER,
)
from glowderma.core.idempotency import already_answered, idempotency_key, idempotent
from glowderma.delivery import selectors as delivery_selectors
from glowderma.integrations.bts.ports import BtsUnavailable
from glowderma.integrations.click.registry import get_click
from glowderma.integrations.payme.registry import get_payme
from glowderma.loyalty import selectors as loyalty_selectors
from glowderma.site_config import selectors as site_config_selectors

BULK_DELETE_MAX = 50
FAKE_SCENARIO_HEADER = "X-GD-Fake-Scenario"
PRODUCT_ID_PARAM = OpenApiParameter(
    name="product_id",
    type=OpenApiTypes.INT,
    location=OpenApiParameter.QUERY,
    many=True,
    required=True,
    description="1..50 product ids (repeat the parameter).",
)


def fake_scenario_header(request: Request) -> dict[str, Any] | None:
    """``X-GD-Fake-Scenario`` (customer contract §1.2): honoured only under the local/test settings with
    ``FAKE_SCENARIO_HEADER_ENABLED`` and every provider mode fake; anything else (or malformed JSON) →
    ``None``."""
    raw = request.headers.get(FAKE_SCENARIO_HEADER)
    if not raw or not integration_modes.dev_features_allowed():
        return None
    if not getattr(settings, "FAKE_SCENARIO_HEADER_ENABLED", False):
        return None
    if any(integration_modes.provider_mode(p) != "fake" for p in ("eskiz", "click", "payme", "bts", "fcm")):
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _customer(request: Request) -> Customer:
    return cast(Customer, request.user)


def _field(name: str, code: str, **kwargs: Any) -> ValidationError:
    return ValidationError({name: [field_error(code, **kwargs)]})


def _cart_response(view: CustomerAPIView, last_change: dict[str, Any] | None = None) -> Response:
    cart = selectors.cart_view(_customer(view.request).pk, view.language, last_change=last_change)
    return Response(CustomerCartSerializer(cart).data)


def _detail_response(order: Order, lang: str, status: int = 200) -> Response:
    detail = selectors.customer_order_detail(order, lang)
    return Response(CustomerOrderDetailSerializer(detail).data, status=status)


def _own_order(customer: Customer, order_id: int) -> Order:
    order = selectors.customer_order(order_id, customer.pk)
    if order is None:
        raise NotFound
    return order


def _consent_current(customer: Customer) -> None:
    """Checkout refuses an outdated consent (customer contract §2.2.4): 409 ``terms_update_required``."""
    current = site_config_selectors.outdated_consent(customer.terms_version)
    if current is not None:
        raise TermsUpdateRequired(details={"current_version": current})


def _delivery_address(
    customer: Customer, address_id: int | None, region_code: str | None, city_code: str | None
) -> tuple[int | None, str, str]:
    """Rows 3 of checkout / the address checks of ``delivery/calculate/``: delivery offered, an own address or
    a region/city pair, known to BTS's reference list. Returns ``(address_id, region_code, city_code)``."""
    if not integration_modes.delivery_available():
        raise DeliveryUnavailable(key="error.delivery_not_offered")
    if address_id is not None and (region_code or city_code):  # exactly one of the two (§2.6)
        raise _field("address_id", "required", message_key="field.address_required")
    if address_id is not None:
        address = Address.objects.filter(pk=address_id, customer_id=customer.pk).first()
        if address is None:
            raise _field("address_id", "does_not_exist", message_key="field.address_not_found")
        region, city = address.region_code, address.city_code
    elif region_code and city_code:
        region, city = region_code, city_code
    else:
        raise _field("address_id", "required", message_key="field.address_required")
    try:
        known = delivery_selectors.region_known(region, city)
    except BtsUnavailable as exc:
        raise ProviderUnavailable() from exc
    if not known:
        field = "address_id" if address_id is not None else "region_code"
        raise _field(field, "address_incomplete", message_key="field.address_incomplete")
    return address_id, region, city


def _localized_lines(lines: list[dict[str, Any]], lang: str) -> list[dict[str, Any]]:
    from glowderma.catalog.models import Product

    products = {p.pk: p for p in Product.objects.filter(pk__in=[line["product_id"] for line in lines])}
    return [
        {**line, "name": selectors.product_name(products[line["product_id"]], lang) or line["name"]}
        if line["product_id"] in products
        else line
        for line in lines
    ]


def _refresh_stock(request: Request, product_ids: list[int], *, key_required: bool) -> services.StockCheck:
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


def _return_url(order_id: int) -> str:
    base = str(settings.PAYMENT_RETURN_URL)
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}{urlencode({'order_id': order_id})}"


# --- cart (customer contract §2.5)
# -----------------------------------------------------------------------------


class CartView(CustomerAPIView):
    @extend_schema(
        operation_id="customer_cart_retrieve",
        parameters=DEVICE_HEADERS,
        responses={200: CustomerCartSerializer, **CUSTOMER_ERRORS},
    )
    def get(self, request: Request) -> Response:
        return _cart_response(self)

    @extend_schema(
        operation_id="customer_cart_clear",
        parameters=[*DEVICE_HEADERS, OPTIONAL_IDEMPOTENCY_KEY_HEADER],
        responses={200: CustomerCartSerializer, **CUSTOMER_ERRORS},
    )
    @idempotent(required=False)
    def delete(self, request: Request) -> Response:
        services.clear_cart(_customer(request).pk)
        return _cart_response(self)


class CartItemsView(CustomerAPIView):
    @extend_schema(
        operation_id="customer_cart_items_create",
        parameters=[*DEVICE_HEADERS, IDEMPOTENCY_KEY_HEADER],
        request=CustomerCartAddSerializer,
        responses={200: CustomerCartSerializer, **CUSTOMER_ERRORS},
    )
    def post(self, request: Request) -> Response:
        # stale stock re-read from Billz (§2.5); never refuses; nothing read for a body that cannot pass
        body = CustomerCartAddSerializer(data=request.data)
        if services.stock_freshness_on() and body.is_valid():
            _refresh_stock(request, [body.validated_data["product_id"]], key_required=True)
        return self._add(request)

    @idempotent()
    def _add(self, request: Request) -> Response:
        if not integration_modes.online_sales_enabled():
            raise OnlineSalesUnavailable()
        body = CustomerCartAddSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        try:
            change = services.add_to_cart(
                _customer(request).pk, body.validated_data["product_id"], body.validated_data["quantity"]
            )
        except services.ProductNotFound as exc:
            raise _field("product_id", "does_not_exist") from exc
        return _cart_response(self, change)

    @extend_schema(
        operation_id="customer_cart_items_bulk_delete",
        parameters=[*DEVICE_HEADERS, PRODUCT_ID_PARAM, OPTIONAL_IDEMPOTENCY_KEY_HEADER],
        request=None,
        responses={200: CustomerCartSerializer, **CUSTOMER_ERRORS},
    )
    @idempotent(required=False)
    def delete(self, request: Request) -> Response:
        raw = request.query_params.getlist("product_id")
        if not raw:
            raise _field("product_id", "required")
        if len(raw) > BULK_DELETE_MAX:
            raise _field("product_id", "max_items", limit=BULK_DELETE_MAX)
        try:
            ids = [int(value) for value in raw]
        except ValueError as exc:
            raise _field("product_id", "invalid") from exc
        services.remove_from_cart(_customer(request).pk, ids)
        return _cart_response(self)


class CartItemView(CustomerAPIView):
    @extend_schema(
        operation_id="customer_cart_items_update",
        parameters=[*DEVICE_HEADERS, OPTIONAL_IDEMPOTENCY_KEY_HEADER],
        request=CustomerCartQuantitySerializer,
        responses={200: CustomerCartSerializer, **CUSTOMER_ERRORS},
    )
    def patch(self, request: Request, product_id: int) -> Response:
        # stale stock re-read from Billz (§2.5); never refuses; only a line already in the cart, valid body
        if (
            services.stock_freshness_on()
            and CustomerCartQuantitySerializer(data=request.data).is_valid()
            and product_id in selectors.cart_product_ids(_customer(request).pk)
        ):
            _refresh_stock(request, [product_id], key_required=False)
        return self._set_quantity(request, product_id)

    @idempotent(required=False)
    def _set_quantity(self, request: Request, product_id: int) -> Response:
        if not integration_modes.online_sales_enabled():
            raise OnlineSalesUnavailable()
        body = CustomerCartQuantitySerializer(data=request.data)
        body.is_valid(raise_exception=True)
        try:
            change = services.set_cart_quantity(
                _customer(request).pk, product_id, body.validated_data["quantity"]
            )
        except (services.CartLineNotFound, services.ProductNotFound) as exc:
            raise NotFound from exc
        return _cart_response(self, change)

    @extend_schema(
        operation_id="customer_cart_items_destroy",
        parameters=[*DEVICE_HEADERS, OPTIONAL_IDEMPOTENCY_KEY_HEADER],
        responses={200: CustomerCartSerializer, **CUSTOMER_ERRORS},
    )
    @idempotent(required=False)
    def delete(self, request: Request, product_id: int) -> Response:
        services.remove_from_cart(_customer(request).pk, [product_id])
        return _cart_response(self)


# --- delivery/calculate/ (customer contract §2.6)
# ----------------------------------------------------------------


class DeliveryCalculateView(CustomerAPIView):
    @extend_schema(
        operation_id="customer_delivery_calculate",
        parameters=[*DEVICE_HEADERS, OPTIONAL_IDEMPOTENCY_KEY_HEADER],
        request=CustomerDeliveryCalculateSerializer,
        responses={200: CustomerDeliveryQuoteSerializer, **CUSTOMER_ERRORS},
    )
    @idempotent(required=False)
    def post(self, request: Request) -> Response:
        body = CustomerDeliveryCalculateSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data
        customer = _customer(request)
        delivery = data["fulfilment"] == enums.FulfilmentMethod.DELIVERY
        region = city = ""
        if delivery:
            _, region, city = _delivery_address(
                customer, data["address_id"], data["region_code"], data["city_code"]
            )
        cart = selectors.cart_view(customer.pk, self.language)
        if not cart["items"]:
            raise CartEmpty()
        fee = 0
        if delivery:
            scenario = fake_scenario_header(request) or {}
            bts = scenario.get("bts") if isinstance(scenario.get("bts"), dict) else None
            fee = services.quote_delivery(region, city, bts)
        subtotal = int(cart["items_subtotal"])
        available = loyalty_selectors.account_summary(customer.pk).available
        maximum = selectors.cashback_max(subtotal, fee, available)
        to_use = min(int(data["cashback_to_use"] or 0), maximum)
        quote = {
            "fulfilment": data["fulfilment"],
            "delivery_fee": fee,
            "region_code": region or None,
            "city_code": city or None,
            "weight_g": int(settings.BTS_DEFAULT_PARCEL_WEIGHT_G) if delivery else None,
            "pickup_address": None if delivery else selectors.pickup_address(self.language),
            "quoted_at": timeutils.now(),
            "items_subtotal": subtotal,
            "cashback_max": maximum,
            "cashback_to_use": to_use,
            "expected_total": subtotal + fee - to_use,
        }
        return Response(CustomerDeliveryQuoteSerializer(quote).data)


# --- checkout, pay, cancel, reorder (customer contract §2.7)
# -----------------------------------------------------


class CheckoutView(CustomerAPIView):
    throttle_classes = [CustomerRateThrottle, CheckoutThrottle]

    @extend_schema(
        operation_id="customer_orders_checkout",
        parameters=[*DEVICE_HEADERS, IDEMPOTENCY_KEY_HEADER],
        request=CustomerCheckoutSerializer,
        responses={201: CustomerOrderDetailSerializer, **CUSTOMER_ERRORS},
    )
    def post(self, request: Request) -> Response:
        # §2.7.1 row 4a: the cart's stale lines are re-read from Billz before the transaction; the refusal
        # (503) is decided in services.checkout after the BTS quote. A body that cannot pass reads nothing.
        stock = services.FRESH
        if (
            _customer(request).phone  # a phoneless account is refused (phone_required): nothing is read
            and services.stock_freshness_on()
            and CustomerCheckoutSerializer(data=request.data).is_valid()
        ):
            stock = _refresh_stock(
                request, selectors.cart_product_ids(_customer(request).pk), key_required=True
            )
        return self._checkout(request, stock)

    @idempotent()
    def _checkout(self, request: Request, stock: services.StockCheck) -> Response:
        if not integration_modes.online_sales_enabled():  # row 0: nothing read or quoted
            raise OnlineSalesUnavailable()
        account_services.require_phone(_customer(request))  # row 0a: hybrid identity, C§2.2.10
        body = CustomerCheckoutSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data
        delivery = data["fulfilment"] == enums.FulfilmentMethod.DELIVERY
        if data["payment_provider"] not in integration_modes.payable_providers():
            raise _field("payment_provider", "invalid_choice")
        if delivery and data["address_id"] is None:
            raise _field("address_id", "required", message_key="field.address_required")
        if not delivery and data["address_id"] is not None:
            raise _field("address_id", "invalid")
        customer = _customer(request)
        _consent_current(customer)
        region = city = ""
        if delivery:
            _, region, city = _delivery_address(customer, data["address_id"], None, None)
        device = self.device
        claims = request.auth if isinstance(request.auth, dict) else {}
        session = AuthSession.objects.filter(pk=str(claims.get("sid", ""))).first()
        try:
            order_id = services.checkout(
                customer.pk,
                services.CheckoutInput(
                    fulfilment=data["fulfilment"],
                    payment_provider=data["payment_provider"],
                    channel=device.platform if device is not None else enums.OrderChannel.IOS,
                    expected_total=data["expected_total"],
                    cashback_to_use=data["cashback_to_use"],
                    address_id=data["address_id"],
                    region_code=region,
                    city_code=city,
                    customer_note=data["customer_note"],
                    scenario=fake_scenario_header(request),
                    checkout_meta={
                        "app_version": device.app_version if device is not None else "",
                        "device_id": session.device_id if session is not None else None,
                    },
                ),
                stock=stock,
            )
        except services.OutOfStock as exc:
            raise OutOfStockError(details={"lines": _localized_lines(exc.lines, self.language)}) from exc
        except services.CashbackAboveMax as exc:
            raise _field(
                "cashback_to_use", "max_value", message_key="field.cashback_max", params={"max": exc.max}
            ) from exc
        except services.PriceChanged as exc:
            cart = CustomerCartSerializer(selectors.cart_view(customer.pk, self.language)).data
            raise PriceChanged(details={**exc.totals, "cart": cart}) from exc
        except services.InsufficientCashback as exc:
            raise _field(
                "cashback_to_use",
                "insufficient_balance",
                message_key="field.cashback_insufficient",
                params={"available": exc.available},
            ) from exc
        except services.AddressNotFound as exc:
            raise _field("address_id", "does_not_exist", message_key="field.address_not_found") from exc
        except services.AddressIncomplete as exc:
            raise _field("address_id", "address_incomplete", message_key="field.address_incomplete") from exc
        order = selectors.customer_order(order_id, customer.pk)
        assert order is not None  # noqa: S101 - just created
        return _detail_response(order, self.language, status=201)


class PayView(CustomerAPIView):
    throttle_classes = [CustomerRateThrottle, PayThrottle]

    @extend_schema(
        operation_id="customer_orders_pay",
        parameters=[*DEVICE_HEADERS, IDEMPOTENCY_KEY_HEADER],
        request=CustomerPaySerializer,
        responses={200: CustomerPayLinkSerializer, **CUSTOMER_ERRORS},
    )
    @idempotent()
    def post(self, request: Request, pk: int) -> Response:
        customer = _customer(request)
        account_services.require_phone(customer)  # hybrid identity, C§2.2.10
        _own_order(customer, pk)
        body = CustomerPaySerializer(data=request.data)
        body.is_valid(raise_exception=True)
        order = services.lock_order(pk)  # every state read below runs under the Order lock
        now = timeutils.now()
        if order.status == enums.OrderStatus.CANCELLED:  # also while a refund of its payment runs
            raise NotPayable()
        if order.status in state.PAID_STATUSES or selectors.honoured_paid(order.pk):
            raise AlreadyPaid()
        if not selectors.is_payable(order, None, now, attaching=True):
            raise OrderExpired()
        provider = body.validated_data["provider"] or order.payment_provider
        flying = selectors.inflight_payment(order.pk, now)
        if flying is not None and flying.provider != provider:
            raise PaymentInProgress(
                details={"provider": flying.provider, "order_id": order.pk},
                params={"provider": selectors.provider_brand(flying.provider)},
            )
        if provider not in integration_modes.payable_providers():
            raise _field("provider", "invalid_choice")
        payment = services.ensure_payable_payment(order.pk, provider, at=now)
        reference = payment_ref.build(order.pk)
        return_url = _return_url(order.pk)
        scenario = services.fake_scenario(order, provider)
        if provider == enums.PaymentProvider.CLICK:
            web_url = get_click(scenario).pay_url(reference, payment.amount, return_url)
        else:
            web_url = get_payme(scenario).checkout_url(
                reference, payment.amount * 100, self.language, return_url
            )
        link = {
            "order_id": order.pk,
            "payment_id": payment.id,
            "provider": provider,
            "amount": payment.amount,
            "pay_by": order.reservation_expires_at,
            "account_ref": reference,
            "web_url": web_url,
            "app_url": None,
            "return_url": return_url,
        }
        return Response(CustomerPayLinkSerializer(link).data)


class CancelView(CustomerAPIView):
    @extend_schema(
        operation_id="customer_orders_cancel",
        parameters=[*DEVICE_HEADERS, IDEMPOTENCY_KEY_HEADER],
        request=None,
        responses={200: CustomerOrderDetailSerializer, **CUSTOMER_ERRORS},
    )
    @idempotent()
    def post(self, request: Request, pk: int) -> Response:
        customer = _customer(request)
        _own_order(customer, pk)
        try:
            services.cancel_by_customer(pk, customer.pk)
        except services.OrderNotFound as exc:
            raise NotFound from exc
        return _detail_response(_own_order(customer, pk), self.language)


class ReorderView(CustomerAPIView):
    @extend_schema(
        operation_id="customer_orders_reorder",
        parameters=[*DEVICE_HEADERS, IDEMPOTENCY_KEY_HEADER],
        request=None,
        responses={200: CustomerCartSerializer, **CUSTOMER_ERRORS},
    )
    @idempotent()
    def post(self, request: Request, pk: int) -> Response:
        customer = _customer(request)
        _own_order(customer, pk)
        services.reorder_into_cart(pk, customer.pk)
        return _cart_response(self)


# --- the customer's orders (customer contract §2.8)
# ----------------------------------------------------------


class MyOrdersViewSet(CustomerGenericViewSet):
    page_size_options = (10, 20)
    default_page_size = 10

    def _now(self) -> datetime:
        return timeutils.now()

    @extend_schema(
        operation_id="customer_orders_list",
        parameters=[*DEVICE_HEADERS, CustomerOrderScopeQuerySerializer],
        responses={200: CustomerOrderSummarySerializer(many=True), **CUSTOMER_ERRORS},
    )
    def list(self, request: Request) -> Response:
        query = CustomerOrderScopeQuerySerializer(data=request.query_params.dict())
        query.is_valid(raise_exception=True)
        orders = selectors.customer_orders(_customer(request).pk, query.validated_data["scope"])
        page = self.paginate_queryset(orders)
        now = self._now()
        rows = [selectors.order_summary(order, self.language, now) for order in page or []]
        serializer: Any = CustomerOrderSummarySerializer(cast(Any, rows), many=True)
        return self.get_paginated_response(serializer.data)

    @extend_schema(
        operation_id="customer_orders_retrieve",
        parameters=DEVICE_HEADERS,
        responses={200: CustomerOrderDetailSerializer, **CUSTOMER_ERRORS},
    )
    def retrieve(self, request: Request, pk: int) -> Response:
        return _detail_response(_own_order(_customer(request), int(pk)), self.language)


__all__ = [
    "CancelView",
    "CartItemView",
    "CartItemsView",
    "CartView",
    "CheckoutView",
    "DeliveryCalculateView",
    "MyOrdersViewSet",
    "PayView",
    "ReorderView",
    "fake_scenario_header",
]
