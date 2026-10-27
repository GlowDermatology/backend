import hashlib
import re
import unicodedata
from typing import Any

from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import EmailValidator
from rest_framework.request import Request
from rest_framework.throttling import SimpleRateThrottle

from glowderma.accounts.authentication import FID_PATTERN
from glowderma.accounts.models import Customer
from glowderma.core.pii import normalize_email, pii_ref
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


ASCII_PHONE = re.compile(r"\+998[0-9]{9}")
INVALID_PHONE_BUCKET = "invalid"


def canonical_phone(value: object) -> str | None:
    """The ASCII E.164 form (``+998`` and nine ASCII digits) of a submitted phone, or ``None``.

    Every Unicode decimal digit (``str.isdecimal``: Arabic-Indic, fullwidth, …, exactly what a Unicode
    ``\\d`` matches) is folded to ASCII first, so look-alike spellings of one number share one canonical
    value and therefore one per-phone throttle key (customer contract §1.8: the per-phone rules are the main
    protection of the auth flows).
    """
    if not isinstance(value, str):
        return None
    folded = "".join(str(unicodedata.decimal(char)) if char.isdecimal() else char for char in value.strip())
    return folded if ASCII_PHONE.fullmatch(folded) else None


def phone_throttle_ident(value: object) -> str | None:
    """The per-phone throttle or guard ident of a submitted ``phone``: ``pii_ref`` of the canonical number;
    ``invalid`` for any other present value (fail closed: no spelling escapes the per-phone limits;
    :class:`SmsPhoneThrottle` scopes that bucket per device); ``None`` when the field is absent or blank (the
    view answers 400 ``required``). P1 keys the ``customer_login`` guard with it too."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    canonical = canonical_phone(value)
    return pii_ref(canonical) if canonical is not None else INVALID_PHONE_BUCKET


EMAIL_MAX_LENGTH = 254
_EMAIL_VALIDATOR = EmailValidator()


def canonical_email(value: object) -> str | None:
    """The stored form of a submitted e-mail address (``core.pii.normalize_email``: NFC, lower case, punycode
    domain; ≤254 characters, a valid address by Django's ``EmailValidator``), or ``None`` (hybrid identity:
    one account and one throttle key per mailbox however it is written)."""
    if not isinstance(value, str):
        return None
    folded = normalize_email(value)
    if not folded or len(folded) > EMAIL_MAX_LENGTH:
        return None
    try:
        _EMAIL_VALIDATOR(folded)
    except DjangoValidationError:
        return None
    return folded


def email_throttle_ident(value: object) -> str | None:
    """:func:`phone_throttle_ident` for an e-mail: ``pii_ref`` of the canonical address, ``invalid`` for any
    other present value, ``None`` when absent or blank."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    canonical = canonical_email(value)
    return pii_ref(canonical) if canonical is not None else INVALID_PHONE_BUCKET


def identifier_throttle_ident(data: object) -> str | None:
    """The per-identifier ident of an auth body: its ``phone`` when present, else its ``email``."""
    if not isinstance(data, dict):
        return None
    phone = phone_throttle_ident(data.get("phone"))
    return phone if phone is not None else email_throttle_ident(data.get("email"))


class SmsPhoneThrottle(ClientIPThrottle):
    """``auth/send-code/`` (and the signed-in ``me/phone/code/`` / ``me/email/code/``) per target: the phone
    (:func:`phone_throttle_ident`: the canonical number, never the raw string, so Unicode-digit variants of
    one number share its key) or, without one, the e-mail address (:func:`email_throttle_ident`, hybrid
    identity).

    The ``invalid`` bucket is split per device (else per client IP): a malformed phone never reaches the SMS
    provider (400 ``invalid``), so one client sending malformed values cannot turn every other user's typo
    into a 429 (``sms_device`` and ``sms_ip`` still bound that client)."""

    scope = "sms_phone"

    def get_cache_key(self, request: Request, view: Any) -> str | None:
        ident = identifier_throttle_ident(request.data)
        if ident is None:
            return None
        if ident == INVALID_PHONE_BUCKET:
            ident = f"{INVALID_PHONE_BUCKET}:{device_ref(request) or self.get_ident(request)}"
        return self.cache_format % {"scope": self.scope, "ident": ident}


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
