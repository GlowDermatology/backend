"""Customer throttle classes (customer contract §1.8): device-first keys, IP backstops, refresh misses."""

from types import SimpleNamespace
from typing import Any

import pytest
from django.test import override_settings
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory
from rest_framework.views import APIView

from glowderma.accounts.api import throttles
from glowderma.accounts.models import Customer
from glowderma.accounts.tests.factories import device_headers
from glowderma.core.api.parsers import JSONParser
from glowderma.core.pii import pii_ref
from glowderma.core.throttling import DEFAULT_THROTTLE_RATES

VIEW = APIView()


def request(user: Any = None, data: Any = None, **headers: str) -> Request:
    raw = APIRequestFactory().post("/", data or {}, format="json", **headers)
    raw.client_ip = "203.0.113.9"  # type: ignore[attr-defined]
    drf = Request(raw, parsers=[JSONParser()])
    loose: Any = drf
    loose.user = user if user is not None else SimpleNamespace(pk=None, is_authenticated=False)
    return drf


def customer(pk: int = 5) -> Customer:
    return Customer(pk=pk, phone="+998901234567")


def test_every_customer_throttle_has_a_default_scope() -> None:
    classes = [
        throttles.CustomerRateThrottle,
        throttles.CheckoutThrottle,
        throttles.PayThrottle,
        throttles.QrThrottle,
        throttles.PublicDeviceThrottle,
        throttles.PublicIPThrottle,
        throttles.SmsPhoneThrottle,
        throttles.SmsDeviceThrottle,
        throttles.SmsIPThrottle,
        throttles.CustomerAuthDeviceThrottle,
        throttles.CustomerAuthIPThrottle,
        throttles.RefreshUnknownThrottle,
        throttles.PosRateThrottle,
    ]
    for cls in classes:
        assert cls.scope in DEFAULT_THROTTLE_RATES, cls.__name__
        assert cls().rate == DEFAULT_THROTTLE_RATES[str(cls.scope)]
    assert DEFAULT_THROTTLE_RATES["sms_ip"] == "600/min"


def test_customer_scoped_throttles_key_on_the_customer_and_skip_anonymous() -> None:
    for cls in (
        throttles.CustomerRateThrottle,
        throttles.CheckoutThrottle,
        throttles.PayThrottle,
        throttles.QrThrottle,
    ):
        assert cls().get_cache_key(request(customer(7)), VIEW) == f"throttle_{cls.scope}_7"
        assert cls().get_cache_key(request(), VIEW) is None


def test_public_throttles_key_on_the_device_then_the_ip_for_anonymous_callers_only() -> None:
    fid_ref = pii_ref("fid-public-0001")
    with_device = request(**device_headers(fid="fid-public-0001"))
    assert (
        throttles.PublicDeviceThrottle().get_cache_key(with_device, VIEW) == f"throttle_public_fid:{fid_ref}"
    )
    assert throttles.PublicDeviceThrottle().get_cache_key(request(), VIEW) == "throttle_public_ip:203.0.113.9"
    assert throttles.PublicIPThrottle().get_cache_key(with_device, VIEW) == "throttle_public_ip_203.0.113.9"
    signed_in = request(customer(), **device_headers())
    assert throttles.PublicDeviceThrottle().get_cache_key(signed_in, VIEW) is None
    assert throttles.PublicIPThrottle().get_cache_key(signed_in, VIEW) is None
    assert "fid-public-0001" not in str(throttles.PublicDeviceThrottle().get_cache_key(with_device, VIEW))


def test_auth_throttles_key_on_the_hashed_fid_phone_and_ip() -> None:
    req = request(data={"phone": " +998901234567 "}, **device_headers(fid="fid-auth-00001"))
    assert (
        throttles.SmsPhoneThrottle().get_cache_key(req, VIEW)
        == f"throttle_sms_phone_{pii_ref('+998901234567')}"
    )
    assert (
        throttles.SmsDeviceThrottle().get_cache_key(req, VIEW)
        == f"throttle_sms_device_{pii_ref('fid-auth-00001')}"
    )
    assert throttles.SmsIPThrottle().get_cache_key(req, VIEW) == "throttle_sms_ip_203.0.113.9"
    assert str(throttles.CustomerAuthDeviceThrottle().get_cache_key(req, VIEW)).endswith(
        pii_ref("fid-auth-00001")
    )
    assert (
        throttles.CustomerAuthIPThrottle().get_cache_key(req, VIEW) == "throttle_customer_auth_ip_203.0.113.9"
    )
    bare = request(data={"phone": 5})
    assert throttles.SmsPhoneThrottle().get_cache_key(bare, VIEW) == "throttle_sms_phone_invalid:203.0.113.9"
    assert throttles.SmsPhoneThrottle().get_cache_key(request(data={"phone": "  "}), VIEW) is None
    assert throttles.SmsPhoneThrottle().get_cache_key(request(data={}), VIEW) is None
    assert throttles.SmsDeviceThrottle().get_cache_key(bare, VIEW) is None


def test_pos_throttle_keys_on_the_authenticated_device() -> None:
    assert throttles.PosRateThrottle().get_cache_key(request(SimpleNamespace(pk=3)), VIEW) == "throttle_pos_3"
    assert throttles.PosRateThrottle().get_cache_key(request(), VIEW) is None


@override_settings(THROTTLE_RATES={"refresh_unknown": "2/min"})
def test_refresh_unknown_counts_only_misses() -> None:
    throttle = throttles.RefreshUnknownThrottle()
    req = request()
    for _ in range(5):
        assert throttles.RefreshUnknownThrottle().allow_request(req, VIEW)  # hits are never counted
    throttle.record_miss(req)
    throttle.record_miss(req)
    blocked = throttles.RefreshUnknownThrottle()
    assert not blocked.allow_request(req, VIEW)
    assert (blocked.wait() or 0) > 0


@pytest.mark.django_db
def test_customer_base_view_throttles_per_customer(customer_client: Any) -> None:
    from glowderma.accounts.api.customer_base import CustomerAPIView

    assert CustomerAPIView.throttle_classes == [throttles.CustomerRateThrottle]


@pytest.mark.parametrize(
    "spelling",
    [
        "+998901234567",
        " +998901234567 ",
        "+998\u0669\u0660\u0661\u0662\u0663\u0664\u0665\u0666\u0667",  # Arabic-Indic 9 0 1 2 3 4 5 6 7
        "+998\uff19\uff10\uff11\uff12\uff13\uff14\uff15\uff16\uff17",  # fullwidth
        "+99890123456\u0667",  # mixed
        "+\u0669\u0669\u0668901234567",  # the country code too
    ],
)
def test_every_unicode_digit_spelling_of_a_phone_shares_one_sms_phone_key(spelling: str) -> None:
    """Review finding (security): the per-phone key is the canonical ASCII number, so Unicode-digit
    look-alikes of one number cannot multiply its ``sms_phone`` budget (customer contract §1.8)."""
    assert throttles.canonical_phone(spelling) == "+998901234567"
    key = throttles.SmsPhoneThrottle().get_cache_key(request(data={"phone": spelling}), VIEW)
    assert key == f"throttle_sms_phone_{pii_ref('+998901234567')}"


@pytest.mark.parametrize(
    "value", ["+99890123456", "+9989012345678", "998901234567", "+998 90 123 45 67", "x", 5]
)
def test_values_that_are_not_a_phone_share_one_invalid_bucket(value: object) -> None:
    assert throttles.canonical_phone(value) is None
    assert throttles.phone_throttle_ident(value) == throttles.INVALID_PHONE_BUCKET
    assert throttles.phone_throttle_ident(None) is None and throttles.phone_throttle_ident(" ") is None


@override_settings(THROTTLE_RATES={"sms_phone": "2/min"})
def test_malformed_phones_of_one_device_do_not_throttle_another_devices_typo() -> None:
    """Review nit (fix pass 2): the ``invalid`` bucket is per device (else per IP), so a client flooding
    malformed phones cannot make an honest typo answer 429 instead of 400 ``invalid``."""
    noisy = request(data={"phone": "x"}, **device_headers(fid="fid-noisy-0001"))
    for _ in range(2):
        assert throttles.SmsPhoneThrottle().allow_request(noisy, VIEW)
    assert not throttles.SmsPhoneThrottle().allow_request(noisy, VIEW)
    typo = request(data={"phone": "+99890123456"}, **device_headers(fid="fid-honest-001"))
    assert throttles.SmsPhoneThrottle().allow_request(typo, VIEW)
    key = str(throttles.SmsPhoneThrottle().get_cache_key(noisy, VIEW))
    assert key == f"throttle_sms_phone_invalid:{pii_ref('fid-noisy-0001')}" and "fid-noisy" not in key
