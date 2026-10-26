import hashlib
from typing import Any

from rest_framework.request import Request
from rest_framework.throttling import SimpleRateThrottle

from glowderma.accounts.authentication import FID_PATTERN
from glowderma.accounts.models import Customer
from glowderma.core.pii import pii_ref
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


class OAuthIPThrottle(LoginRateThrottle):
    """Contract 1.9: ``auth/oauth/{provider}/`` per client IP, 30/min by default."""

    scope = "oauth"


# --- Customer API (customer contract §1.8) ---
# Carrier-grade NAT puts thousands of app users behind a few IPs, so customer throttles key on the device
# first (``pii_ref`` of the fid, never the raw value) and keep a high pure-IP ceiling as a backstop.


def device_ref(request: Request) -> str | None:
    """``pii_ref`` of a well-formed ``X-GD-Device-Id`` (else ``None``)."""
    fid = str(request.headers.get("X-GD-Device-Id", ""))
    return pii_ref(fid) if FID_PATTERN.fullmatch(fid) else None


def _is_customer(request: Request) -> bool:
    return isinstance(getattr(request, "user", None), Customer)


class CustomerRateThrottle(ClientIPThrottle):
    """Every C route (and an O route called with a valid customer token): per customer id."""

    scope = "customer"

    def get_cache_key(self, request: Request, view: Any) -> str | None:
        if not _is_customer(request):
            return None
        return self.cache_format % {"scope": self.scope, "ident": str(request.user.pk)}


class CheckoutThrottle(CustomerRateThrottle):
    scope = "checkout"


class PayThrottle(CustomerRateThrottle):
    scope = "pay"


class QrThrottle(CustomerRateThrottle):
    scope = "qr"


class PublicDeviceThrottle(ClientIPThrottle):
    """Anonymous O requests: per device when the header is present, else per client IP (signed-in callers
    count against ``customer``)."""

    scope = "public"

    def get_cache_key(self, request: Request, view: Any) -> str | None:
        if _is_customer(request):
            return None
        ref = device_ref(request)
        ident = f"fid:{ref}" if ref is not None else f"ip:{self.get_ident(request)}"
        return self.cache_format % {"scope": self.scope, "ident": ident}


class PublicIPThrottle(ClientIPThrottle):
    """Anonymous O requests: the per-IP backstop."""

    scope = "public_ip"

    def get_cache_key(self, request: Request, view: Any) -> str | None:
        if _is_customer(request):
            return None
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


class _DeviceThrottle(ClientIPThrottle):
    def get_cache_key(self, request: Request, view: Any) -> str | None:
        ref = device_ref(request)
        if ref is None:  # the view answers 400 device_required
            return None
        return self.cache_format % {"scope": self.scope, "ident": ref}


class SmsPhoneThrottle(ClientIPThrottle):
    """``auth/send-code/`` per phone (``pii_ref`` of the submitted value)."""

    scope = "sms_phone"

    def get_cache_key(self, request: Request, view: Any) -> str | None:
        data = request.data
        phone = data.get("phone") if isinstance(data, dict) else None
        if not isinstance(phone, str) or not phone.strip():
            return None
        return self.cache_format % {"scope": self.scope, "ident": pii_ref(phone.strip())}


class SmsDeviceThrottle(_DeviceThrottle):
    scope = "sms_device"


class SmsIPThrottle(LoginRateThrottle):
    scope = "sms_ip"


class CustomerAuthDeviceThrottle(_DeviceThrottle):
    scope = "customer_auth_device"


class CustomerAuthIPThrottle(LoginRateThrottle):
    scope = "customer_auth_ip"


class RefreshUnknownThrottle(LoginRateThrottle):
    """``auth/refresh/`` per client IP, counting **only** refresh tokens that match no session.

    ``allow_request`` refuses once the window is full but never counts; the refresh view calls
    :meth:`record_miss` when the presented token matched nothing, so junk-token rotation cannot bypass the
    per-token ``refresh`` key while honest refreshes never use this budget.
    """

    scope = "refresh_unknown"
    # Set by SimpleRateThrottle.__init__ from the parsed rate (not declared by the stubs).
    num_requests: int
    duration: int

    def _recent(self, key: str) -> list[float]:
        now = self.timer()
        return [moment for moment in self.cache.get(key, []) if moment > now - self.duration]

    def allow_request(self, request: Request, view: Any) -> bool:
        if self.rate is None:
            return True
        self.key = self.get_cache_key(request, view)
        self.now = self.timer()
        self.history = self._recent(self.key)
        if len(self.history) >= self.num_requests:
            return self.throttle_failure()
        return True

    def record_miss(self, request: Request, view: Any = None) -> None:
        key = self.get_cache_key(request, view)
        history = self._recent(key)
        history.insert(0, self.timer())
        self.cache.set(key, history, self.duration)


class PosRateThrottle(ClientIPThrottle):
    """``pos/*`` per POS device, after a successful key authentication (``request.user`` is the device)."""

    scope = "pos"

    def get_cache_key(self, request: Request, view: Any) -> str | None:
        device = getattr(request, "user", None)
        pk = getattr(device, "pk", None)
        if pk is None:
            return None
        return self.cache_format % {"scope": self.scope, "ident": str(pk)}
