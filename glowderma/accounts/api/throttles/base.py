"""The base of every throttle: the rate comes from ``core.throttling`` (``THROTTLE_RATES`` overrides) and the
cache key is ``throttle_<scope>_<ident>``. Subclasses name their ``scope`` and, unless they count per client
IP, override :meth:`ClientIPThrottle.cache_ident` (Template Method): ``None`` means the throttle does not
apply to the request."""

from __future__ import annotations

from typing import Any

from rest_framework.request import Request
from rest_framework.throttling import SimpleRateThrottle

from glowderma.core.throttling import rate_for


class ClientIPThrottle(SimpleRateThrottle):
    def get_rate(self) -> str:
        return rate_for(self.scope)

    def get_ident(self, request: Request) -> str:
        return str(getattr(request, "client_ip", request.META.get("REMOTE_ADDR", "")))

    def cache_ident(self, request: Request, view: Any) -> str | None:
        """What the budget is counted per: the client IP unless a subclass says otherwise."""
        return self.get_ident(request)

    def get_cache_key(self, request: Request, view: Any) -> str | None:
        ident = self.cache_ident(request, view)
        if ident is None:
            return None
        return self.cache_format % {"scope": self.scope, "ident": ident}


__all__ = ["ClientIPThrottle"]
