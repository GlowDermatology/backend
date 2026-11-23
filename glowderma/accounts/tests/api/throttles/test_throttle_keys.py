"""Characterization of every throttle's cache key (customer contract §1.8, admin §1.9): the keys are part of
the running system (a deploy must not reset or split anyone's budget), so a refactor of the throttle classes
must keep each one byte-identical. Every class is resolved by name from the module that owns it."""

from __future__ import annotations

import hashlib
import importlib
from types import SimpleNamespace
from typing import Any

import pytest
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from glowderma.accounts.models import Customer
from glowderma.accounts.tests.factories import device_headers
from glowderma.core.api.parsers import JSONParser
from glowderma.core.pii import pii_ref

FID = device_headers()["HTTP_X_GD_DEVICE_ID"]
PHONE = "+998901234567"


def throttle(name: str) -> Any:
    homes = {
        "CheckoutThrottle": "glowderma.commerce.api.customer.throttles",
        "PayThrottle": "glowderma.commerce.api.customer.throttles",
        "DeliveryQuoteThrottle": "glowderma.commerce.api.customer.throttles",
        "QrThrottle": "glowderma.loyalty.api.throttles",
        "PosRateThrottle": "glowderma.loyalty.api.throttles",
    }
    for module in (homes.get(name), "glowderma.accounts.api.throttles"):
        if module is None:
            continue
        try:
            return getattr(importlib.import_module(module), name)()
        except ModuleNotFoundError, AttributeError:
            continue
    raise LookupError(name)


def make(
    *, data: Any = None, user: Any = None, headers: dict[str, str] | None = None, ip: str = "203.0.113.9"
) -> Request:
    factory = APIRequestFactory()
    django_request = factory.post("/x/", data or {}, format="json", **(headers or {}))
    django_request.client_ip = ip  # type: ignore[attr-defined]
    request = Request(django_request, parsers=[JSONParser()])
    if user is not None:
        request.user = user
    return request


VIEW = SimpleNamespace(kwargs={})
STAFF = SimpleNamespace(pk=7)
CUSTOMER = Customer(pk=11, phone=PHONE)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("LoginRateThrottle", "throttle_login_203.0.113.9"),
        ("AuthClientIPThrottle", "throttle_auth_client_ip_203.0.113.9"),
        ("OAuthIPThrottle", "throttle_oauth_203.0.113.9"),
        ("PasswordTokenIPThrottle", "throttle_staff_password_token_203.0.113.9"),
        ("MfaVerifyIPThrottle", "throttle_mfa_verify_203.0.113.9"),
        ("SmsIPThrottle", "throttle_sms_ip_203.0.113.9"),
        ("CustomerAuthIPThrottle", "throttle_customer_auth_ip_203.0.113.9"),
        ("RefreshUnknownThrottle", "throttle_refresh_unknown_203.0.113.9"),
    ],
)
def test_per_client_ip_keys(name: str, expected: str) -> None:
    assert throttle(name).get_cache_key(make(), VIEW) == expected


def test_refresh_key_is_the_token_digest() -> None:
    digest = hashlib.sha256(b"r-token").hexdigest()
    assert throttle("RefreshRateThrottle").get_cache_key(make(data={"refresh": "r-token"}), VIEW) == (
        f"throttle_refresh_{digest}"
    )


def test_staff_keys() -> None:
    assert throttle("StaffRateThrottle").get_cache_key(make(user=STAFF), VIEW) == "throttle_staff_7"
    link = throttle("PasswordLinkThrottle")
    assert link.get_cache_key(make(user=STAFF), VIEW) == "throttle_staff_password_link_7:7"
    assert link.get_cache_key(make(user=STAFF), SimpleNamespace(kwargs={"pk": 9})) == (
        "throttle_staff_password_link_7:9"
    )


@pytest.mark.parametrize(
    ("name", "scope"),
    [
        ("CustomerRateThrottle", "customer"),
        ("CheckoutThrottle", "checkout"),
        ("PayThrottle", "pay"),
        ("QrThrottle", "qr"),
    ],
)
def test_per_customer_keys(name: str, scope: str) -> None:
    assert throttle(name).get_cache_key(make(user=CUSTOMER), VIEW) == f"throttle_{scope}_11"
    assert throttle(name).get_cache_key(make(user=STAFF), VIEW) is None


def test_delivery_quote_counts_only_carrier_requests() -> None:
    quote = throttle("DeliveryQuoteThrottle")
    assert quote.get_cache_key(make(user=CUSTOMER, data={"with_options": True}), VIEW) == (
        "throttle_delivery_quote_11"
    )
    assert quote.get_cache_key(make(user=CUSTOMER, data={"address_id": 1}), VIEW) is None


def test_public_keys() -> None:
    device = make(headers=device_headers())
    assert (
        throttle("PublicDeviceThrottle").get_cache_key(device, VIEW) == f"throttle_public_fid:{pii_ref(FID)}"
    )
    assert throttle("PublicDeviceThrottle").get_cache_key(make(), VIEW) == "throttle_public_ip:203.0.113.9"
    assert throttle("PublicIPThrottle").get_cache_key(make(), VIEW) == "throttle_public_ip_203.0.113.9"
    assert throttle("PublicBffThrottle").get_cache_key(make(), VIEW) is None
    assert throttle("PublicDeviceThrottle").get_cache_key(make(user=CUSTOMER), VIEW) is None


@pytest.mark.parametrize("name", ["SmsDeviceThrottle", "CustomerAuthDeviceThrottle"])
def test_per_device_keys(name: str) -> None:
    scope = throttle(name).scope
    assert (
        throttle(name).get_cache_key(make(headers=device_headers()), VIEW)
        == f"throttle_{scope}_{pii_ref(FID)}"
    )
    assert throttle(name).get_cache_key(make(), VIEW) is None


def test_sms_phone_keys() -> None:
    sms = throttle("SmsPhoneThrottle")
    assert sms.get_cache_key(make(data={"phone": PHONE}), VIEW) == f"throttle_sms_phone_{pii_ref(PHONE)}"
    assert sms.get_cache_key(make(data={"phone": "x"}, headers=device_headers()), VIEW) == (
        f"throttle_sms_phone_invalid:{pii_ref(FID)}"
    )
    assert sms.get_cache_key(make(data={"phone": "x"}), VIEW) == "throttle_sms_phone_invalid:203.0.113.9"
    assert sms.get_cache_key(make(data={}), VIEW) is None


def test_pos_keys() -> None:
    pos = throttle("PosRateThrottle")
    assert pos.get_cache_key(make(user=SimpleNamespace(pk=3)), VIEW) == "throttle_pos_3"
    assert pos.get_cache_key(make(user=SimpleNamespace()), VIEW) is None
