"""The throttles of anonymous optional-auth (O) routes: per device or client IP, a pure-IP backstop, and one
bucket for the website BFF's own calls (signed-in callers count against ``customer``)."""

from __future__ import annotations

from typing import Any

from rest_framework.request import Request

from glowderma.accounts.api.throttles import idents
from glowderma.accounts.api.throttles.base import ClientIPThrottle


class PublicDeviceThrottle(ClientIPThrottle):
    """Anonymous O requests: per device when the header is present, else per client IP (signed-in callers
    count against ``customer``). The client IP is the BFF-forwarded one when trusted (``core.clientip``)."""

    scope = "public"

    def cache_ident(self, request: Request, view: Any) -> str | None:
        if idents.is_customer(request):
            return None
        ref = idents.device_ref(request)
        if idents.bff_own_call(request) and ref is None:
            return None  # counted by PublicBffThrottle
        return f"fid:{ref}" if ref is not None else f"ip:{self.get_ident(request)}"


class PublicIPThrottle(ClientIPThrottle):
    """Anonymous O requests: the per-IP backstop (the browser's IP behind the website's BFF; not applied to
    the BFF's own calls, which ``PublicBffThrottle`` bounds)."""

    scope = "public_ip"

    def cache_ident(self, request: Request, view: Any) -> str | None:
        if idents.is_customer(request) or idents.bff_own_call(request):
            return None
        return self.get_ident(request)


class PublicBffThrottle(ClientIPThrottle):
    """B-W1: anonymous O requests the website's BFF makes on its own behalf (``idents.bff_own_call``), one
    generous bucket so a runaway revalidation loop is still bounded."""

    scope = "public_bff"

    def cache_ident(self, request: Request, view: Any) -> str | None:
        if idents.is_customer(request) or not idents.bff_own_call(request):
            return None
        return "bff"


__all__ = ["PublicBffThrottle", "PublicDeviceThrottle", "PublicIPThrottle"]
