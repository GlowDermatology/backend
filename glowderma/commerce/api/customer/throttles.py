"""Throttles of commerce's customer routes (customer contract §1.8): checkout, pay and the carrier-asking
delivery quote, each per customer."""

from __future__ import annotations

from typing import Any

from rest_framework.request import Request

from glowderma.accounts.api.throttles import CustomerRateThrottle


class CheckoutThrottle(CustomerRateThrottle):
    scope = "checkout"


class PayThrottle(CustomerRateThrottle):
    scope = "pay"


class DeliveryQuoteThrottle(CustomerRateThrottle):
    """``delivery/calculate/`` requests that ask the carriers (any Yandex plan §3.4 key): each may call
    Yandex check-price once per enabled class on the shared business account. The released request (none of
    the keys) only reads the cached BTS tariff and stays under the plain ``customer`` scope."""

    scope = "delivery_quote"
    CARRIER_KEYS = ("with_options", "delivery_provider", "delivery_method")

    def cache_ident(self, request: Request, view: Any) -> str | None:
        data = request.data
        if not isinstance(data, dict) or not any(data.get(key) for key in self.CARRIER_KEYS):
            return None
        return super().cache_ident(request, view)


__all__ = ["CheckoutThrottle", "DeliveryQuoteThrottle", "PayThrottle"]
