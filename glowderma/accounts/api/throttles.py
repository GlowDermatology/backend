import hashlib
from typing import Any

from rest_framework.request import Request
from rest_framework.throttling import SimpleRateThrottle

from glowderma.core.throttling import rate_for


class ClientIPThrottle(SimpleRateThrottle):
    def get_rate(self) -> str:
        return rate_for(self.scope)

    def get_ident(self, request: Request) -> str:
        return str(getattr(request, "client_ip", request.META.get("REMOTE_ADDR", "")))


class LoginRateThrottle(ClientIPThrottle):
    scope = "login"

    def get_cache_key(self, request: Request, view: Any) -> str:
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


class AuthClientIPThrottle(LoginRateThrottle):
    scope = "auth_client_ip"


class RefreshRateThrottle(ClientIPThrottle):
    scope = "refresh"

    def get_cache_key(self, request: Request, view: Any) -> str:
        data = request.data
        refresh = str(data.get("refresh", "")) if isinstance(data, dict) else ""
        return self.cache_format % {
            "scope": self.scope,
            "ident": hashlib.sha256(refresh.encode()).hexdigest(),
        }


class StaffRateThrottle(ClientIPThrottle):
    scope = "staff"

    def get_cache_key(self, request: Request, view: Any) -> str:
        return self.cache_format % {"scope": self.scope, "ident": str(request.user.pk)}
