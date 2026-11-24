"""``delivery/calculate/`` (customer contract §2.6): the delivery options and price of the cart."""

from __future__ import annotations

from typing import Any

from django.conf import settings
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.customer_base import CustomerAPIView
from glowderma.accounts.api.throttles import CustomerRateThrottle
from glowderma.accounts.models import Address, Customer
from glowderma.commerce import quoting, selectors, services
from glowderma.commerce.api.customer.schemas.delivery_quote import DELIVERY_CALCULATE_POST
from glowderma.commerce.api.customer.serializers import (
    CustomerDeliveryCalculateSerializer,
    CustomerDeliveryQuoteBaseSerializer,
    CustomerDeliveryQuoteSerializer,
)
from glowderma.commerce.api.customer.throttles import DeliveryQuoteThrottle
from glowderma.commerce.api.customer.views.common import (
    fake_scenario_header,
    field_refusal,
    signed_in_customer,
)
from glowderma.commerce.api.customer.views.destinations import address_snapshot, delivery_address
from glowderma.commerce.api.customer.views.quote_answers import options_data
from glowderma.core import enums, integration_modes, timeutils
from glowderma.core.api.exceptions import (
    CartEmpty,
    DeliveryUnavailable,
    ProviderUnavailable,
)
from glowderma.core.idempotency import already_answered, idempotent
from glowderma.loyalty import selectors as loyalty_selectors


def _wants_options(data: dict[str, Any]) -> bool:
    """A request with any plan §3.4 key (``with_options``, ``delivery_provider``, ``delivery_method``) gets
    the options answer; without them ``delivery/calculate/`` answers exactly as the released app expects."""
    return bool(data.get("with_options") or data.get("delivery_provider") or data.get("delivery_method"))


def _options_address(
    customer: Customer, address_id: int | None, region_code: str | None, city_code: str | None
) -> quoting.AddressSnapshot:
    """The destination of the options answer: a saved address (its pin serves Yandex, its BTS codes BTS) or a
    bare region/city pair (BTS only). No BTS codes are required here: each option says what it lacks."""
    if not integration_modes.delivery_providers():
        raise DeliveryUnavailable(key="error.delivery_not_offered")
    if address_id is not None and (region_code or city_code):  # exactly one of the two (§2.6)
        raise field_refusal("address_id", "required", message_key="field.address_required")
    if address_id is not None:
        address = Address.objects.filter(pk=address_id, customer_id=customer.pk).first()
        if address is None:
            raise field_refusal("address_id", "does_not_exist", message_key="field.address_not_found")
        return address_snapshot(address)
    if region_code and city_code:
        return quoting.AddressSnapshot(
            address_id=None,
            text="",
            latitude=None,
            longitude=None,
            region_code=region_code,
            city_code=city_code,
        )
    raise field_refusal("address_id", "required", message_key="field.address_required")


class DeliveryCalculateView(CustomerAPIView):
    throttle_classes = [CustomerRateThrottle, DeliveryQuoteThrottle]

    @DELIVERY_CALCULATE_POST
    def post(self, request: Request) -> Response:
        # plan §3.4: the carriers are asked here, before the @idempotent transaction (which only stores and
        # returns the answer); a replayed key asks nobody
        body = CustomerDeliveryCalculateSerializer(data=request.data)
        prepared = None
        if body.is_valid() and _wants_options(body.validated_data) and not already_answered(request):
            prepared = self._options_answer(request, body.validated_data)
        return self._calculate(request, prepared)

    def _options_answer(self, request: Request, data: dict[str, Any]) -> dict[str, Any]:
        """The superset answer: the released keys for the selected option plus ``provider``, ``method``,
        ``quote_id``, ``quote_expires_at`` and ``options[]``. Same checks, in the same order, as the released
        path (address, then the cart)."""
        customer = signed_in_customer(request)
        delivery = data["fulfilment"] == enums.FulfilmentMethod.DELIVERY
        snapshot = (
            _options_address(customer, data["address_id"], data["region_code"], data["city_code"])
            if delivery
            else None
        )
        cart = selectors.cart_view(customer.pk, self.language)
        if not cart["items"]:
            raise CartEmpty()
        now = timeutils.now()
        subtotal = int(cart["items_subtotal"])
        available = loyalty_selectors.account_summary(customer.pk).available
        wanted = int(data["cashback_to_use"] or 0)
        selected: quoting.DeliveryOption | None = None
        options: list[quoting.DeliveryOption] = []
        if snapshot is not None:
            options = quoting.delivery_options(
                quoting.QuoteRequest(
                    customer_id=customer.pk,
                    address=snapshot,
                    quantity=int(cart["quantity_total"]),
                    items_subtotal=subtotal,
                    cashback_available=available,
                    cashback_to_use=wanted,
                    lang=self.language,
                    now=now,
                    scenario=fake_scenario_header(request),
                )
            )
            selected = quoting.select_option(options, data["delivery_provider"], data["delivery_method"])
            if selected is None or selected.fee is None:
                raise DeliveryUnavailable(
                    key="error.delivery_unavailable", details={"options": options_data(options)}
                )
        fee = selected.fee if selected is not None and selected.fee is not None else 0
        maximum = selectors.cashback_max(subtotal, fee, available)
        to_use = min(wanted, maximum)
        quote = {
            "fulfilment": data["fulfilment"],
            "delivery_fee": fee,
            "region_code": (snapshot.region_code or None) if snapshot is not None else None,
            "city_code": (snapshot.city_code or None) if snapshot is not None else None,
            "weight_g": selected.weight_g if selected is not None else None,
            "pickup_address": None if delivery else selectors.pickup_address(self.language),
            "quoted_at": now,
            "items_subtotal": subtotal,
            "cashback_max": maximum,
            "cashback_to_use": to_use,
            "expected_total": subtotal + fee - to_use,
            "provider": selected.provider if selected is not None else None,
            "method": selected.method if selected is not None else None,
            "quote_id": selected.quote_id if selected is not None else None,
            "quote_expires_at": selected.expires_at if selected is not None else None,
            "options": [option.as_dict() for option in options],
        }
        return dict(CustomerDeliveryQuoteSerializer(quote).data)

    @idempotent(required=False)
    def _calculate(self, request: Request, prepared: dict[str, Any] | None) -> Response:
        if prepared is not None:
            return Response(prepared)
        body = CustomerDeliveryCalculateSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data
        if _wants_options(data):  # pragma: no cover - only a key whose stored answer expired mid-request
            raise ProviderUnavailable()
        customer = signed_in_customer(request)
        delivery = data["fulfilment"] == enums.FulfilmentMethod.DELIVERY
        region = city = ""
        if delivery:
            _, region, city = delivery_address(
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
        return Response(CustomerDeliveryQuoteBaseSerializer(quote).data)


__all__ = [
    "DeliveryCalculateView",
]
