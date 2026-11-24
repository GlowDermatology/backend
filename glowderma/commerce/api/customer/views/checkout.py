"""Checkout (customer contract §2.7): the order from the cart, its delivery quote and the cashback."""

from __future__ import annotations

from typing import Any

from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts import services as account_services
from glowderma.accounts.api.customer_base import CustomerAPIView
from glowderma.accounts.api.throttles import CustomerRateThrottle
from glowderma.accounts.models import Address, AuthSession, Customer
from glowderma.commerce import quoting, selectors, services
from glowderma.commerce.api.customer.schemas.checkout import CHECKOUT_POST
from glowderma.commerce.api.customer.serializers import (
    CustomerCartSerializer,
    CustomerCheckoutSerializer,
)
from glowderma.commerce.api.customer.throttles import CheckoutThrottle
from glowderma.commerce.api.customer.views.common import (
    fake_scenario_header,
    field_refusal,
    order_detail_response,
    refresh_stock,
    signed_in_customer,
)
from glowderma.commerce.api.customer.views.destinations import address_snapshot, delivery_address
from glowderma.commerce.api.customer.views.quote_answers import options_data
from glowderma.core import enums, integration_modes, timeutils
from glowderma.core.api.exceptions import (
    CustomerError,
    OnlineSalesUnavailable,
    PriceChanged,
    TermsUpdateRequired,
)
from glowderma.core.api.exceptions import OutOfStock as OutOfStockError
from glowderma.core.idempotency import already_answered, idempotency_key, idempotent
from glowderma.loyalty import selectors as loyalty_selectors
from glowderma.site_config import selectors as site_config_selectors


def _consent_current(customer: Customer) -> None:
    """Checkout refuses an outdated consent (customer contract §2.2.4): 409 ``terms_update_required``."""
    current = site_config_selectors.outdated_consent(customer.terms_version)
    if current is not None:
        raise TermsUpdateRequired(details={"current_version": current})


def _quote_expired(exc: quoting.QuoteExpired) -> CustomerError:
    """409 ``quote_expired``; ``details.options`` carries the fresh options when a re-quote ran."""
    details = {"options": options_data(exc.options)} if exc.options is not None else None
    return CustomerError("quote_expired", 409, details=details)


def _localized_lines(lines: list[dict[str, Any]], lang: str) -> list[dict[str, Any]]:
    from glowderma.catalog.models import Product

    products = {p.pk: p for p in Product.objects.filter(pk__in=[line["product_id"] for line in lines])}
    return [
        {**line, "name": selectors.product_name(products[line["product_id"]], lang) or line["name"]}
        if line["product_id"] in products
        else line
        for line in lines
    ]


class CheckoutView(CustomerAPIView):
    throttle_classes = [CustomerRateThrottle, CheckoutThrottle]

    @CHECKOUT_POST
    def post(self, request: Request) -> Response:
        # §2.7.1 row 4a: the cart's stale lines are re-read from Billz before the transaction; the refusal
        # (503) is decided in services.checkout after the BTS quote. A body that cannot pass reads nothing.
        stock = services.FRESH
        if (
            signed_in_customer(
                request
            ).phone  # a phoneless account is refused (phone_required): nothing is read
            and services.stock_freshness_on()
            and CustomerCheckoutSerializer(data=request.data).is_valid()
        ):
            stock = refresh_stock(
                request, selectors.cart_product_ids(signed_in_customer(request).pk), key_required=True
            )
        # plan §3.4: a Yandex quote is checked (and re-quoted when it no longer fits) before the transaction
        return self._checkout(request, stock, self._yandex_quote(request))

    def _yandex_quote(self, request: Request) -> quoting.CheckoutQuote | None:
        """The quote of a ``delivery_quote_id`` checkout, checked against the address, the cart and the
        method settings as they are now; a stale one is re-quoted (an equal fee proceeds, another fee → 409
        ``price_changed`` with the fresh options, nothing available → 409 ``quote_expired``). ``None`` — and
        no carrier is asked — for a request the transaction will refuse or replay anyway."""
        body = CustomerCheckoutSerializer(data=request.data)
        if not body.is_valid():
            return None
        data = body.validated_data
        if data["delivery_quote_id"] is None or data["fulfilment"] != enums.FulfilmentMethod.DELIVERY:
            return None
        customer = signed_in_customer(request)
        if (
            data["address_id"] is None
            or not integration_modes.online_sales_enabled()
            or not customer.phone
            or data["payment_provider"] not in integration_modes.payable_providers()
            or site_config_selectors.outdated_consent(customer.terms_version) is not None
            or idempotency_key(request) is None
            or already_answered(request)
        ):
            return None
        address = Address.objects.filter(pk=data["address_id"], customer_id=customer.pk).first()
        if address is None:
            return None
        cart = selectors.cart_view(customer.pk, self.language)
        if not cart["items"]:
            return None
        subtotal = int(cart["items_subtotal"])
        wanted = int(data["cashback_to_use"] or 0)
        quote_request = quoting.QuoteRequest(
            customer_id=customer.pk,
            address=address_snapshot(address),
            quantity=int(cart["quantity_total"]),
            items_subtotal=subtotal,
            cashback_available=loyalty_selectors.account_summary(customer.pk).available,
            cashback_to_use=wanted,
            lang=self.language,
            now=timeutils.now(),
            scenario=fake_scenario_header(request),
        )
        try:
            return quoting.checkout_quote(quote_request, data["delivery_quote_id"])
        except quoting.DeliveryFeeChanged as exc:
            fee = int(exc.option.fee or 0)
            # row 8 before row 9, as in create_order: a cashback the new fee no longer allows is the
            # transaction's own refusal, never a "new total" built on it (negative or below the minimum)
            maximum = selectors.cashback_max(subtotal, fee, None)
            if wanted > maximum:
                raise field_refusal(
                    "cashback_to_use", "max_value", message_key="field.cashback_max", params={"max": maximum}
                ) from exc
            raise PriceChanged(
                key="error.delivery_price_changed",
                details={
                    "total": subtotal + fee - wanted,
                    "items_subtotal": subtotal,
                    "delivery_fee": fee,
                    "cashback_used": wanted,
                    "cart": CustomerCartSerializer(cart).data,
                    "quote_id": str(exc.option.quote_id) if exc.option.quote_id is not None else None,
                    "options": options_data(exc.options),
                },
            ) from exc
        except quoting.QuoteExpired as exc:
            raise _quote_expired(exc) from exc

    @idempotent()
    def _checkout(
        self, request: Request, stock: services.StockCheck, prepared: quoting.CheckoutQuote | None
    ) -> Response:
        if not integration_modes.online_sales_enabled():  # row 0: nothing read or quoted
            raise OnlineSalesUnavailable()
        account_services.require_phone(signed_in_customer(request))  # row 0a: hybrid identity, C§2.2.10
        body = CustomerCheckoutSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data
        delivery = data["fulfilment"] == enums.FulfilmentMethod.DELIVERY
        if data["payment_provider"] not in integration_modes.payable_providers():
            raise field_refusal("payment_provider", "invalid_choice")
        if delivery and data["address_id"] is None:
            raise field_refusal("address_id", "required", message_key="field.address_required")
        if not delivery and data["address_id"] is not None:
            raise field_refusal("address_id", "invalid")
        if not delivery and data["delivery_quote_id"] is not None:
            raise field_refusal("delivery_quote_id", "invalid")
        customer = signed_in_customer(request)
        _consent_current(customer)
        region = city = ""
        quote_id = None
        if delivery and data["delivery_quote_id"] is not None:
            # Yandex: an own saved address (its pin), no BTS region/city; the quote was checked in post()
            if not Address.objects.filter(pk=data["address_id"], customer_id=customer.pk).exists():
                raise field_refusal("address_id", "does_not_exist", message_key="field.address_not_found")
            # None when post() asked no carrier (an emptied cart, a key whose stored answer expired): the
            # transaction re-checks the quote under the customer lock (own, unused, unexpired, same address,
            # method usable, weight), so a used quote stays quote_expired while an empty cart is cart_empty
            quote_id = prepared.quote_id if prepared is not None else data["delivery_quote_id"]
        elif delivery:
            _, region, city = delivery_address(customer, data["address_id"], None, None)
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
                    delivery_quote_id=quote_id,
                ),
                stock=stock,
            )
        except quoting.QuoteExpired as exc:
            raise _quote_expired(exc) from exc
        except services.OutOfStock as exc:
            raise OutOfStockError(details={"lines": _localized_lines(exc.lines, self.language)}) from exc
        except services.CashbackAboveMax as exc:
            raise field_refusal(
                "cashback_to_use", "max_value", message_key="field.cashback_max", params={"max": exc.max}
            ) from exc
        except services.PriceChanged as exc:
            cart = CustomerCartSerializer(selectors.cart_view(customer.pk, self.language)).data
            raise PriceChanged(details={**exc.totals, "cart": cart}) from exc
        except services.InsufficientCashback as exc:
            raise field_refusal(
                "cashback_to_use",
                "insufficient_balance",
                message_key="field.cashback_insufficient",
                params={"available": exc.available},
            ) from exc
        except services.AddressNotFound as exc:
            raise field_refusal(
                "address_id", "does_not_exist", message_key="field.address_not_found"
            ) from exc
        except services.AddressIncomplete as exc:
            raise field_refusal(
                "address_id", "address_incomplete", message_key="field.address_incomplete"
            ) from exc
        order = selectors.customer_order(order_id, customer.pk)
        assert order is not None  # noqa: S101 - just created
        return order_detail_response(order, self.language, status=201)


__all__ = [
    "CheckoutView",
]
