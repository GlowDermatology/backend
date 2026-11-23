"""Throttles of loyalty's routes: the customer's QR token (per customer) and the POS terminals (per
device)."""

from __future__ import annotations

from typing import Any

from rest_framework.request import Request

from glowderma.accounts.api.throttles import ClientIPThrottle, CustomerRateThrottle


class QrThrottle(CustomerRateThrottle):
    scope = "qr"


class PosRateThrottle(ClientIPThrottle):
    """``pos/*`` per POS device, after a successful key authentication (``request.user`` is the device)."""

    scope = "pos"

    def cache_ident(self, request: Request, view: Any) -> str | None:
        pk = getattr(getattr(request, "user", None), "pk", None)
        return str(pk) if pk is not None else None


__all__ = ["PosRateThrottle", "QrThrottle"]
