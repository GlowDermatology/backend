"""Throttles of the customer sign-in flows (customer contract §1.8): the per-identifier one-time-code budget,
and the per-device and per-IP budgets of the auth routes."""

from __future__ import annotations

from typing import Any

from rest_framework.request import Request

from glowderma.accounts.api.throttles import idents
from glowderma.accounts.api.throttles.base import ClientIPThrottle
from glowderma.accounts.api.throttles.customer import PerDeviceThrottle
from glowderma.accounts.security import identifiers


class SmsPhoneThrottle(ClientIPThrottle):
    """``auth/send-code/`` (and the signed-in ``me/phone/code/`` / ``me/email/code/``) per target: the phone
    (:func:`identifiers.phone_throttle_ident`: the canonical number, never the raw string, so Unicode-digit
    variants of one number share its key) or, without one, the e-mail address
    (:func:`identifiers.email_throttle_ident`, hybrid identity).

    The ``invalid`` bucket is split per device (else per client IP): a malformed phone never reaches the SMS
    provider (400 ``invalid``), so one client sending malformed values cannot turn every other user's typo
    into a 429 (``sms_device`` and ``sms_ip`` still bound that client)."""

    scope = "sms_phone"

    def cache_ident(self, request: Request, view: Any) -> str | None:
        ident = identifiers.identifier_throttle_ident(request.data)
        if ident == identifiers.INVALID_IDENTIFIER_BUCKET:
            scoped_to = idents.device_ref(request) or self.get_ident(request)
            return f"{identifiers.INVALID_IDENTIFIER_BUCKET}:{scoped_to}"
        return ident


class SmsDeviceThrottle(PerDeviceThrottle):
    scope = "sms_device"


class SmsIPThrottle(ClientIPThrottle):
    scope = "sms_ip"


class CustomerAuthDeviceThrottle(PerDeviceThrottle):
    scope = "customer_auth_device"


class CustomerAuthIPThrottle(ClientIPThrottle):
    scope = "customer_auth_ip"


__all__ = [
    "CustomerAuthDeviceThrottle",
    "CustomerAuthIPThrottle",
    "SmsDeviceThrottle",
    "SmsIPThrottle",
    "SmsPhoneThrottle",
]
