"""The live Uzum Checkout adapter and its protocol helpers (docs/integrations/uzum.md): the requests it
sends, the answers it reads and every failure mapping. No test reaches the network."""

import json
import logging

import httpx
import pytest

from glowderma.integrations import httpclient
from glowderma.integrations.uzum import protocol
from glowderma.integrations.uzum.live import LiveUzum
from glowderma.integrations.uzum.ports import UzumNotProcessed, UzumRejected, UzumUnavailable

from .live_fixtures import Script, connect_error, load, read_timeout, reply

BASE = "https://checkout.example.uz"
TERMINAL = "11111111-2222-4333-8444-555555555555"
API_KEY = "66666666-7777-4888-9999-000000000000"
ORDER_ID = "3f2b8c1e-6d4a-4b7e-9c11-0a1b2c3d4e5f"


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpclient, "RETRY_BACKOFF_SECONDS", 0)


def uzum(script: Script, hosts: list[str] | None = None) -> LiveUzum:
    return LiveUzum(
        base_url=f"{BASE}/",
        terminal_id=TERMINAL,
        api_key=API_KEY,
        redirect_hosts=hosts,
        transport=script.transport,
    )


def register(adapter: LiveUzum, language: str = "uz") -> object:
    return adapter.register(
        order_number="gd-41-0a1b2c3d",
        amount=25_000_000,
        client_id="c" * 32,
        language=language,
        success_url="https://glowderma.uz/pay/return?order_id=7",
        failure_url="https://glowderma.uz/pay/return?order_id=7",
        session_timeout=600,
    )


# --- protocol --------------------------------------------------------------------------------------------


def test_amounts_are_tiyin_and_compared_exactly() -> None:
    assert protocol.to_tiyin(250_000) == 25_000_000
    assert protocol.matches_som(25_000_000, 250_000)
    assert not protocol.matches_som(25_000_001, 250_000)  # one tiyin off
    assert not protocol.matches_som(250_000, 250_000)  # so'm sent as tiyin: 1 % of the price
    assert not protocol.matches_som(True, 1)
    with pytest.raises(ValueError):
        protocol.to_tiyin(0)


def test_order_numbers_fit_36_characters_and_carry_the_environment_prefix() -> None:
    number = protocol.new_order_number("gd", 999_999_999_999_999_999)
    assert len(number) <= protocol.ORDER_NUMBER_MAX
    assert protocol.payment_id_of(number, "gd") == 999_999_999_999_999_999
    assert protocol.payment_id_of(number, "stg") is None  # another environment's terminal
    assert protocol.payment_id_of("gd-12", "gd") is None
    assert protocol.new_order_number("gd", 5) != protocol.new_order_number("gd", 5)  # fresh per register


def test_content_language_is_one_of_uzums_three_locales() -> None:
    assert protocol.content_language("en") == "en-EN"
    assert protocol.content_language("ru") == "ru-RU"
    assert protocol.content_language("xx") == "uz-UZ"


def test_the_operation_id_is_stable_per_refund() -> None:
    assert protocol.operation_id(5) == protocol.operation_id(5)
    assert protocol.operation_id(5) != protocol.operation_id(6)
    assert protocol.is_uuid(protocol.operation_id(5))


def test_only_https_payment_pages_on_the_allowed_hosts_are_trusted() -> None:
    assert protocol.redirect_url_ok("https://checkout.uzumbank.uz/pay/x", [])
    assert protocol.redirect_url_ok("https://checkout.uzumbank.uz/pay/x", ["checkout.uzumbank.uz"])
    assert not protocol.redirect_url_ok("https://evil.example/pay/x", ["checkout.uzumbank.uz"])
    assert not protocol.redirect_url_ok("http://checkout.uzumbank.uz/pay/x", [])
    assert not protocol.redirect_url_ok("https://user:pw@checkout.uzumbank.uz/", [])
    assert not protocol.redirect_url_ok(None, [])


def test_callback_fields_keep_the_allow_list_only() -> None:
    fields = protocol.callback_fields(
        {
            "orderId": ORDER_ID,
            "orderNumber": "gd-1-0a1b2c3d",
            "rrn": "123",
            "cardType": 2,
            "operationState": 1,
        }
    )
    assert fields == {"orderId": ORDER_ID, "orderNumber": "gd-1-0a1b2c3d"}


# --- register ----------------------------------------------------------------------------------------------


def test_register_sends_a_one_step_redirect_payment_with_the_terminal_headers() -> None:
    script = Script().on("POST", protocol.PATH_REGISTER, reply("uzum", "register_ok"))
    registration = register(uzum(script), language="en")
    assert registration.order_id == ORDER_ID  # type: ignore[attr-defined]
    (request,) = script.requests
    assert str(request.url) == f"{BASE}{protocol.PATH_REGISTER}"
    assert request.headers["X-Terminal-Id"] == TERMINAL
    assert request.headers["X-API-Key"] == API_KEY
    assert request.headers["Content-Language"] == "en-EN"
    body = json.loads(request.content)
    assert body["amount"] == 25_000_000
    assert body["currency"] == 860
    assert body["viewType"] == "REDIRECT"
    assert body["paymentParams"]["payType"] == "ONE_STEP"
    assert body["sessionTimeoutSecs"] == 600
    assert body["successUrl"] == body["failureUrl"]


def test_register_refuses_a_payment_page_off_the_allowed_hosts() -> None:
    script = Script().on("POST", protocol.PATH_REGISTER, reply("uzum", "register_ok"))
    with pytest.raises(UzumRejected) as refused:
        register(uzum(script, hosts=["pay.uzum.uz"]))
    assert refused.value.code == "redirect_refused"


def test_an_error_code_is_a_rejection_with_uzums_code() -> None:
    script = Script().on("POST", protocol.PATH_REGISTER, reply("uzum", "error_3027"))
    with pytest.raises(UzumRejected) as refused:
        register(uzum(script))
    assert refused.value.code == "3027"


@pytest.mark.parametrize(
    ("responder", "error"),
    [
        (connect_error, UzumNotProcessed),
        (httpx.Response(429), UzumNotProcessed),
        (httpx.Response(401), UzumNotProcessed),
        (httpx.Response(302, headers={"Location": "https://elsewhere/"}), UzumNotProcessed),
        (read_timeout, UzumUnavailable),
        (httpx.Response(502), UzumUnavailable),
        (httpx.Response(200, json={"result": {}}), UzumUnavailable),  # no errorCode
        (httpx.Response(200, json={"errorCode": 0, "result": {"orderId": "x"}}), UzumUnavailable),
    ],
)
def test_failures_are_typed(responder: object, error: type[Exception]) -> None:
    script = Script().on("POST", protocol.PATH_REGISTER, responder)  # type: ignore[arg-type]
    with pytest.raises(error):
        register(uzum(script))


def test_a_read_timeout_is_never_resent() -> None:
    script = Script().on("POST", protocol.PATH_REGISTER, read_timeout)
    with pytest.raises(UzumUnavailable) as failure:
        register(uzum(script))
    assert failure.value.sent
    assert len(script.requests) == 1


def test_a_4xx_body_keeps_uzums_code() -> None:
    script = Script().on("POST", protocol.PATH_REGISTER, httpx.Response(400, json={"errorCode": 2000}))
    with pytest.raises(UzumRejected) as refused:
        register(uzum(script))
    assert refused.value.code == "2000"


def test_auth_failures_are_logged_without_the_key(caplog: pytest.LogCaptureFixture) -> None:
    script = Script().on("POST", protocol.PATH_REGISTER, httpx.Response(403))
    with caplog.at_level(logging.INFO), pytest.raises(UzumNotProcessed):
        register(uzum(script))
    assert "uzum.auth_failed" in caplog.text
    assert API_KEY not in caplog.text


# --- status and refund -------------------------------------------------------------------------------------


def test_status_reads_the_amounts_and_operations() -> None:
    script = Script().on("POST", protocol.PATH_ORDER_STATUS, reply("uzum", "status_completed"))
    status = uzum(script).order_status(ORDER_ID)
    assert status.status == "COMPLETED"
    assert status.completed_amount == 25_000_000
    assert status.merchant_order_id == "gd-41-0a1b2c3d"
    assert [op.operation_type for op in status.operations] == ["COMPLETE"]
    assert json.loads(script.requests[0].content) == {"orderId": ORDER_ID}


def test_a_status_answer_for_another_order_is_not_trusted() -> None:
    body = load("uzum", "status_completed")
    body["result"]["orderId"] = "00000000-0000-4000-8000-000000000000"
    script = Script().on("POST", protocol.PATH_ORDER_STATUS, httpx.Response(200, json=body))
    with pytest.raises(UzumUnavailable):
        uzum(script).order_status(ORDER_ID)


def test_a_status_with_an_unknown_value_or_a_missing_amount_is_unknown() -> None:
    for patch in ({"status": "PAID"}, {"completedAmount": "25000000"}):
        body = load("uzum", "status_completed")
        body["result"].update(patch)
        script = Script().on("POST", protocol.PATH_ORDER_STATUS, httpx.Response(200, json=body))
        with pytest.raises(UzumUnavailable):
            uzum(script).order_status(ORDER_ID)


def test_refund_sends_our_operation_id() -> None:
    script = Script().on("POST", protocol.PATH_REFUND, reply("uzum", "refund_ok"))
    operation = protocol.operation_id(9)
    accepted = uzum(script).refund(ORDER_ID, 25_000_000, operation)
    assert accepted.operation_id == "1c2d3e4f-5a6b-4c7d-8e9f-0a1b2c3d4e5f"
    (request,) = script.requests
    assert request.headers["X-Operation-Id"] == operation
    assert json.loads(request.content) == {"orderId": ORDER_ID, "amount": 25_000_000}


def test_calls_with_an_id_that_is_not_uzums_never_leave() -> None:
    script = Script()
    with pytest.raises(UzumRejected):
        uzum(script).order_status("12")
    with pytest.raises(UzumRejected):
        uzum(script).refund("12", 1, protocol.operation_id(1))
    assert script.requests == []


def test_is_configured_needs_https_the_terminal_and_the_key() -> None:
    assert uzum(Script()).is_configured()
    assert not LiveUzum(base_url="http://x", terminal_id=TERMINAL, api_key=API_KEY).is_configured()
    assert not LiveUzum(base_url=BASE, terminal_id="", api_key=API_KEY).is_configured()
