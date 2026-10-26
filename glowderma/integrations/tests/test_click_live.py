"""The live Click Merchant API adapter: signed requests, the status mapping and every failure mapping
(customer contract §2.13.0; docs/integrations/click.md)."""

import hashlib
import logging
import time

import httpx
import pytest
from django.test import override_settings

from glowderma.integrations import httpclient
from glowderma.integrations.click import protocol
from glowderma.integrations.click.fake import FakeClick
from glowderma.integrations.click.live import LiveClick
from glowderma.integrations.click.ports import (
    ClickRefundRejected,
    ClickStatusUnknown,
    ClickUnavailable,
    RefundReceipt,
)
from glowderma.integrations.click.registry import get_click

from .live_fixtures import Script, connect_error, read_timeout, reply

SECRET = "click-secret-key-value"  # noqa: S105 - test value
PAYDOC = "5500010248"
STATUS_PATH = f"/v2/merchant/payment/status/36123/{PAYDOC}"
REVERSAL_PATH = f"/v2/merchant/payment/reversal/36123/{PAYDOC}"


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpclient, "RETRY_BACKOFF_SECONDS", 0)


def click(script: Script) -> LiveClick:
    return LiveClick(
        api_url="https://api.click.uz/v2/merchant/",
        service_id="36123",
        merchant_id="20456",
        merchant_user_id="40789",
        secret_key=SECRET,
        pay_url="https://my.click.uz/services/pay",
        transport=script.transport,
    )


def test_the_auth_header_follows_the_documented_formula() -> None:
    expected = hashlib.sha1(f"1700000000{SECRET}".encode(), usedforsecurity=False).hexdigest()
    assert protocol.merchant_auth_header("40789", SECRET, 1700000000) == f"40789:{expected}:1700000000"


def test_a_status_check_is_a_signed_get_by_paydoc_id() -> None:
    script = Script().on("GET", STATUS_PATH, reply("click", "status_paid"))
    assert click(script).payment_status(PAYDOC) == "paid"
    (request,) = script.requests
    assert str(request.url) == f"https://api.click.uz{STATUS_PATH}"
    assert request.headers["Accept"] == request.headers["Content-Type"] == "application/json"
    user_id, digest, stamp = request.headers["Auth"].split(":")
    assert user_id == "40789" and len(stamp) == 10 and abs(int(stamp) - time.time()) < 60
    assert digest == hashlib.sha1(f"{stamp}{SECRET}".encode(), usedforsecurity=False).hexdigest()
    assert SECRET not in str(request.url) and SECRET not in request.headers["Auth"]


def test_status_codes_map_to_paid_reversed_or_unknown() -> None:
    script = Script().on("GET", STATUS_PATH, reply("click", "status_reversed"))
    assert click(script).payment_status(PAYDOC) == "reversed"
    script = Script().on("GET", STATUS_PATH, reply("click", "status_unknown_code"))
    with pytest.raises(ClickStatusUnknown, match="payment_status 0"):
        click(script).payment_status(PAYDOC)
    assert protocol.merchant_payment_state(2) == protocol.merchant_payment_state("1") == "paid"
    assert protocol.merchant_payment_state(-99) == "reversed"
    for unknown in (0, -1, -5017, None, True, "x", 2.5):
        assert protocol.merchant_payment_state(unknown) is None


@pytest.mark.parametrize(
    ("responder", "error", "match"),
    [
        (reply("click", "status_error"), ClickStatusUnknown, "error_code -16"),
        (httpx.Response(200, text="<html>maintenance</html>"), ClickStatusUnknown, "without error_code"),
        (httpx.Response(404, json={}), ClickStatusUnknown, "HTTP 404"),
        (httpx.Response(503, json={}), ClickUnavailable, "HTTP 503"),
        (read_timeout, ClickUnavailable, "ReadTimeout"),
        (connect_error, ClickUnavailable, "ConnectError"),
    ],
)
def test_a_status_check_never_guesses(responder: object, error: type[Exception], match: str) -> None:
    script = Script().on("GET", STATUS_PATH, responder)  # type: ignore[arg-type]
    with pytest.raises(error, match=match) as raised:
        click(script).payment_status(PAYDOC)
    # every failure is a ClickUnavailable for the callers: no refund is closed or skipped on it
    assert isinstance(raised.value, ClickUnavailable)
    if error is ClickUnavailable:
        assert not isinstance(raised.value, ClickStatusUnknown)


def test_a_refused_signature_is_an_error_log_without_the_secret(caplog: pytest.LogCaptureFixture) -> None:
    script = Script().on("GET", STATUS_PATH, httpx.Response(401, json={}))
    with caplog.at_level(logging.INFO), pytest.raises(ClickStatusUnknown):
        click(script).payment_status(PAYDOC)
    assert any(r.levelno == logging.ERROR and "click.auth_failed" in r.getMessage() for r in caplog.records)
    assert SECRET not in caplog.text and "Auth" not in caplog.text.replace("auth_failed", "")


def test_a_non_numeric_payment_id_is_never_sent() -> None:
    script = Script()
    with pytest.raises(ClickStatusUnknown):
        click(script).payment_status("../../status/1")
    with pytest.raises(ClickRefundRejected) as refused:
        click(script).refund("CLK-1", 784_400)
    assert refused.value.error_code == "invalid_payment_id"
    assert script.requests == []


def test_a_reversal_is_a_signed_delete_by_paydoc_id() -> None:
    script = Script().on("DELETE", REVERSAL_PATH, reply("click", "reversal_ok"))
    assert click(script).refund(PAYDOC, 784_400) == RefundReceipt(provider_refund_id=PAYDOC)
    (request,) = script.requests
    assert request.method == "DELETE" and request.content == b""
    assert request.headers["Auth"].startswith("40789:")


def test_a_refused_reversal_carries_clicks_code_without_personal_data() -> None:
    script = Script().on("DELETE", REVERSAL_PATH, reply("click", "reversal_refused"))
    with pytest.raises(ClickRefundRejected) as refused:
        click(script).refund(PAYDOC, 784_400)
    assert refused.value.error_code == "-5014"
    assert "Reversal is not available" in refused.value.message
    assert "998901234567" not in refused.value.message and "8600" not in refused.value.message


@pytest.mark.parametrize(
    ("responder", "error", "code"),
    [
        (httpx.Response(403, json={}), ClickRefundRejected, "http_403"),
        (httpx.Response(400, text="bad"), ClickRefundRejected, "http_400"),
        (httpx.Response(500, json={}), ClickUnavailable, None),
        (httpx.Response(200, text="not json"), ClickUnavailable, None),
        (read_timeout, ClickUnavailable, None),
        (connect_error, ClickUnavailable, None),
    ],
)
def test_reversal_failures(responder: object, error: type[Exception], code: str | None) -> None:
    script = Script().on("DELETE", REVERSAL_PATH, responder)  # type: ignore[arg-type]
    with pytest.raises(error) as raised:
        click(script).refund(PAYDOC, 784_400)
    if code is not None:
        assert isinstance(raised.value, ClickRefundRejected) and raised.value.error_code == code
    sent = len(script.to("DELETE", REVERSAL_PATH))
    # a reversal that may have reached Click is never repeated by the adapter
    assert sent == (httpclient.RETRY_ATTEMPTS if responder is connect_error else 1)


@override_settings(
    CLICK_MODE="live",
    CLICK_SERVICE_ID="36123",
    CLICK_MERCHANT_ID="20456",
    CLICK_MERCHANT_USER_ID="40789",
    CLICK_SECRET_KEY=SECRET,
    CLICK_MERCHANT_API_URL="https://api.click.uz/v2/merchant",
    CLICK_PAY_URL="https://my.click.uz/services/pay",
)
def test_the_registry_builds_the_live_adapter_and_the_pay_url_is_the_contracts() -> None:
    live = get_click({"refund": "failed"})  # the scenario is ignored by the live adapter
    assert isinstance(live, LiveClick)
    assert (live.service_id, live.merchant_id, live.merchant_user_id) == ("36123", "20456", "40789")
    url = live.pay_url("10248-3f9a0c1b2d", 784_400, "https://glowderma.uz/pay/return?order_id=10248")
    assert url == (
        "https://my.click.uz/services/pay?service_id=36123&merchant_id=20456&amount=784400"
        "&transaction_param=10248-3f9a0c1b2d&return_url=https%3A%2F%2Fglowderma.uz%2Fpay%2Freturn%3Forder_id%3D10248"
    )
    # the fake builds the very same URL (only the refund/status side differs)
    assert (
        FakeClick().pay_url("10248-3f9a0c1b2d", 784_400, "https://glowderma.uz/pay/return?order_id=10248")
        == url
    )
