"""The throttles of signed-in customer routes: one budget per customer, and the per-device base of the routes
a device calls before anyone is signed in."""

from __future__ import annotations

from typing import Any

from rest_framework.request import Request

from glowderma.accounts.api.throttles import idents
from glowderma.accounts.api.throttles.base import ClientIPThrottle


class CustomerRateThrottle(ClientIPThrottle):
    """Every C route (and an O route called with a valid customer token): per customer id."""

    scope = "customer"

    def cache_ident(self, request: Request, view: Any) -> str | None:
        return str(request.user.pk) if idents.is_customer(request) else None


class PerDeviceThrottle(ClientIPThrottle):
    """Per ``X-GD-Device-Id`` (``pii_ref``); without a well-formed one the view answers 400
    ``device_required`` and the throttle does not apply."""

    def cache_ident(self, request: Request, view: Any) -> str | None:
        return idents.device_ref(request)


__all__ = ["CustomerRateThrottle", "PerDeviceThrottle"]
