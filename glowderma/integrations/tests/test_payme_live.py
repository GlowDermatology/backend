"""The live Payme adapter: the checkout URL with the real merchant id and the host of the mode (customer
contract §2.7.2; docs/integrations/payme.md). Payme's money flow is inbound, so no HTTP call is made."""

import base64
from urllib.parse import unquote

import pytest
from django.test import override_settings

from glowderma.integrations.payme.fake import FakePayme
from glowderma.integrations.payme.live import LivePayme
from glowderma.integrations.payme.registry import get_payme

MERCHANT = "587f72c72cac0d162c722ae2"
RETURN = "https://glowderma.uz/pay/return?order_id=10248"


def decoded(url: str, host: str) -> dict[str, str]:
    assert url.startswith(f"{host}/")
    raw = base64.b64decode(url.removeprefix(f"{host}/")).decode()
    return dict(part.split("=", 1) for part in raw.split(";"))


def test_the_checkout_url_carries_the_real_merchant_and_tiyin() -> None:
    payme = LivePayme(
        merchant_id=MERCHANT,
        key="payme-key",
        checkout_url="https://checkout.paycom.uz/",
        account_field="order_id",
    )
    params = decoded(
        payme.checkout_url("10248-3f9a0c1b2d", 78_440_000, "ru", RETURN), "https://checkout.paycom.uz"
    )
    assert params == {
        "m": MERCHANT,
        "ac.order_id": "10248-3f9a0c1b2d",
        "a": "78440000",
        "l": "ru",
        "c": params["c"],
        "ct": "15000",
    }
    assert unquote(params["c"]) == RETURN  # encoded, so ";" and "=" of the return URL never split a parameter
    assert payme.refunds_are_manual is True


@pytest.mark.parametrize(
    ("merchant", "key", "configured"),
    [
        (MERCHANT, "payme-key", True),
        ("", "payme-key", False),
        (MERCHANT, "", False),
        ("change-me-payme-merchant-id", "payme-key", False),
        (MERCHANT, "  ", False),
    ],
)
def test_is_configured_needs_real_values(merchant: str, key: str, configured: bool) -> None:
    payme = LivePayme(
        merchant_id=merchant, key=key, checkout_url="https://checkout.paycom.uz", account_field="x"
    )
    assert payme.is_configured() is configured


@pytest.mark.parametrize(
    ("checkout", "host"),
    [
        ("https://test.paycom.uz", "https://test.paycom.uz"),
        ("https://checkout.paycom.uz", "https://checkout.paycom.uz"),
    ],
)
def test_the_registry_builds_the_live_adapter_on_the_host_of_the_mode(checkout: str, host: str) -> None:
    with override_settings(
        PAYME_MODE="live", PAYME_MERCHANT_ID=MERCHANT, PAYME_KEY="payme-key", PAYME_CHECKOUT_URL=checkout
    ):
        payme = get_payme({"transaction_id": "PME-1"})  # the scenario is ignored by the live adapter
        assert isinstance(payme, LivePayme) and payme.is_configured()
        params = decoded(payme.checkout_url("10248-3f9a0c1b2d", 100_000, "uz", RETURN), host)
    assert params["m"] == MERCHANT and params["ac.order_id"] == "10248-3f9a0c1b2d"


def test_the_fake_is_unchanged() -> None:
    fake = get_payme()
    assert isinstance(fake, FakePayme) and fake.is_configured() and fake.refunds_are_manual
