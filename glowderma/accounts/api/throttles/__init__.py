"""The throttle classes of every API audience, behind one facade: ``base`` (the rate lookup and the
``throttle_<scope>_<ident>`` key, with the ``cache_ident`` hook each throttle overrides), ``staff``,
``customer``, ``public``, ``customer_auth`` and ``refresh_misses``; ``idents`` says who a customer-facing
request counts against. Throttles of other modules' routes live with them (commerce checkout/pay/delivery
quote, loyalty QR and POS), built on :class:`ClientIPThrottle` / :class:`CustomerRateThrottle`.
"""

from glowderma.accounts.api.throttles.base import ClientIPThrottle
from glowderma.accounts.api.throttles.customer import CustomerRateThrottle, PerDeviceThrottle
from glowderma.accounts.api.throttles.customer_auth import (
    CustomerAuthDeviceThrottle,
    CustomerAuthIPThrottle,
    SmsDeviceThrottle,
    SmsIPThrottle,
    SmsPhoneThrottle,
)
from glowderma.accounts.api.throttles.idents import bff_own_call, device_ref, is_customer
from glowderma.accounts.api.throttles.public import PublicBffThrottle, PublicDeviceThrottle, PublicIPThrottle
from glowderma.accounts.api.throttles.refresh_misses import RefreshUnknownThrottle
from glowderma.accounts.api.throttles.staff import (
    AuthClientIPThrottle,
    LoginRateThrottle,
    MfaVerifyIPThrottle,
    OAuthIPThrottle,
    PasswordLinkThrottle,
    PasswordTokenIPThrottle,
    RefreshRateThrottle,
    StaffRateThrottle,
)

__all__ = [
    "AuthClientIPThrottle",
    "ClientIPThrottle",
    "CustomerAuthDeviceThrottle",
    "CustomerAuthIPThrottle",
    "CustomerRateThrottle",
    "LoginRateThrottle",
    "MfaVerifyIPThrottle",
    "OAuthIPThrottle",
    "PasswordLinkThrottle",
    "PasswordTokenIPThrottle",
    "PerDeviceThrottle",
    "PublicBffThrottle",
    "PublicDeviceThrottle",
    "PublicIPThrottle",
    "RefreshRateThrottle",
    "RefreshUnknownThrottle",
    "SmsDeviceThrottle",
    "SmsIPThrottle",
    "SmsPhoneThrottle",
    "StaffRateThrottle",
    "bff_own_call",
    "device_ref",
    "is_customer",
]
