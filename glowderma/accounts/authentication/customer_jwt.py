"""Customer access JWTs (``aud = customer``, customer contract §1.2, §1.9, §2.1.6): the session state from the
``auth:sid:{sid}`` cache entry, the device and platform checks, the rotation confirmation and the throttled
activity bump."""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.core.cache import cache
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.request import Request

from glowderma.accounts import request_facts, services, tokens
from glowderma.accounts.authentication import bearer, device_headers
from glowderma.accounts.models import AuthSession, Customer, Device
from glowderma.core import audit, enums, timeutils
from glowderma.core.api.exceptions import AuthFailed
from glowderma.core.pii import pii_ref

# The device's last_active_at / app_version are written at most this often (seconds).
DEVICE_BUMP_SECONDS = 300
# At most one ``auth.device_mismatch`` audit row per session per this many seconds.
MISMATCH_AUDIT_SECONDS = 3600


def _active_entry(session: AuthSession) -> str:
    device: Device | None = session.device
    fid_ref = pii_ref(device.fid) if device is not None else ""
    platform = device.platform if device is not None else ""
    return f"active:{session.absolute_expires_at.timestamp()}:{fid_ref}:{platform}"


class CustomerJWTAuthentication(BaseAuthentication):
    """Customer access JWT (``aud = customer``, customer contract §1.2, §1.9, §2.1.6).

    The session state comes from ``auth:sid:{sid}``: ``active:{abs_expiry}:{pii_ref(fid)}:{platform}``, filled
    with ``cache.add`` only (never rewritten, so no write can overwrite a ``revoked:<reason>`` tombstone
    written on commit by a revocation), which lets the device and platform checks run without a query. The
    first request carrying the session's current ``rot`` confirms the rotation with one conditional UPDATE,
    remembered by the add-only flag ``auth:sidrot:{sid}:{rot}``. ``last_active_at`` / ``app_version`` of the
    device and the session's activity (``last_used_at``, user agent, IP and its location — B-W1) are bumped
    at most every five minutes.
    """

    def authenticate(self, request: Request) -> tuple[Customer, dict[str, Any]] | None:
        raw_token = bearer.bearer_token(request)
        if raw_token is None:
            if get_authorization_header(request):  # Basic, Api-Key, …: present but not a customer bearer
                raise AuthFailed("not_authenticated")
            return None
        claims = tokens.decode_access(raw_token, audience="customer")
        sid = claims["sid"]
        key = f"auth:sid:{sid}"
        value = cache.get(key)
        if value is None:
            session = AuthSession.objects.select_related("device").filter(pk=sid).first()
            if session is None:
                raise AuthFailed("SESSION_EXPIRED")
            if session.audience != "customer" or session.customer_id != int(claims["sub"]):
                raise AuthFailed("not_authenticated")
            if session.revoked_at is not None:
                services.raise_revoked_session(session.revoke_reason)
            remaining = session.absolute_expires_at.timestamp() - timeutils.now().timestamp()
            if remaining <= 0:
                raise AuthFailed("SESSION_EXPIRED")
            active = _active_entry(session)
            cache.add(key, active, timeout=max(1, min(60, int(remaining))))
            value = cache.get(key, active)
        if isinstance(value, str) and value.startswith("revoked:"):
            services.raise_revoked_session(value.removeprefix("revoked:"))
        try:
            if not isinstance(value, str) or not value.startswith("active:"):
                raise ValueError("Invalid session cache entry")
            absolute, fid_ref, platform = value.removeprefix("active:").rsplit(":", 2)
            if timeutils.now().timestamp() >= float(absolute):
                raise AuthFailed("SESSION_EXPIRED")
        except (ValueError, TypeError) as exc:
            raise AuthFailed("SESSION_EXPIRED") from exc
        customer = Customer.objects.filter(pk=int(claims["sub"])).first()
        if customer is None or customer.status != enums.CustomerStatus.ACTIVE:
            raise AuthFailed("SESSION_EXPIRED")
        device = device_headers.request_device(request) or device_headers.parse_device_headers(
            request, required=False
        )
        if device is None or device.fid_ref != fid_ref or device.platform != platform:
            if (
                device is not None
                and device.fid_ref != fid_ref
                and cache.add(f"auth:mismatch:{sid}", 1, timeout=MISMATCH_AUDIT_SECONDS)
            ):
                audit.record(
                    action="auth.device_mismatch",
                    entity_type="customer",
                    entity_id=str(customer.pk),
                    actor=customer,
                    request=request,
                    after={"sid": sid},
                )
            raise AuthFailed("not_authenticated")
        self._confirm_rotation(sid, int(claims["rot"]))
        if cache.add(f"auth:devbump:{sid}", 1, timeout=DEVICE_BUMP_SECONDS):
            Device.objects.filter(sessions__pk=sid).update(
                last_active_at=timeutils.now(), app_version=device.app_version, updated_at=timeutils.now()
            )
            user_agent, ip = request_facts.activity_facts(request, platform=device.platform)
            services.touch_session(sid, user_agent=user_agent, ip=ip)
        return customer, claims

    @staticmethod
    def _confirm_rotation(sid: str, rot: int) -> None:
        flag = f"auth:sidrot:{sid}:{rot}"
        if cache.get(flag) is not None:
            return
        AuthSession.objects.filter(pk=sid, rotation=rot, rotation_confirmed=False).update(
            rotation_confirmed=True
        )
        cache.add(flag, 1, timeout=int(settings.CUSTOMER_ACCESS_TTL))

    def authenticate_header(self, request: Request) -> str:
        return 'Bearer realm="api"'


# The optional-auth (O) routes use the same class: no ``Authorization`` header → anonymous (``AllowAny`` lets
# the request through); a present header must be a valid customer access token (an invalid one, or another
# scheme, is a 401, never silently anonymous). One class keeps one OpenAPI security component
# (``CustomerJWT``).
OptionalCustomerAuthentication = CustomerJWTAuthentication


__all__ = [
    "DEVICE_BUMP_SECONDS",
    "MISMATCH_AUDIT_SECONDS",
    "CustomerJWTAuthentication",
    "OptionalCustomerAuthentication",
]
