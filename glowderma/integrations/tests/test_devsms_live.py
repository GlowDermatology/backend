# ruff: noqa: S105, S106 - test credentials
"""The live DevSMS adapter against answers shaped as DevSMS API v1 documents them (no network): the request
(universal OTP object, template per purpose, phone format, bearer token, idempotency key, no sender), every
documented outcome mapped to accepted / rejected / repeatable / unknown, and nothing sensitive in a log line
or an exception."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable

import httpx
import pytest
from django.test import override_settings

from glowderma.integrations.devsms import live as devsms_live
from glowderma.integrations.devsms.live import LiveDevsms, provider_phone, template_type, valid_service_name
from glowderma.integrations.devsms.ports import OtpPurpose, OtpSms, SmsDelivery, SmsRejected, SmsUnavailable
from glowderma.integrations.devsms.registry import get_sms_port
from glowderma.integrations.tests.live_fixtures import Script, connect_error, read_timeout, reply

PHONE = "+998901231231"
CODE = "482913"
TOKEN = "devsms-secret-token"
KEY = "3f2b8a52-7c1d-4e0a-9b4f-6d5c1a2e8b70"
SEND = "/api/v1/sms"


def sms(purpose: OtpPurpose = OtpPurpose.REGISTRATION, *, phone: str = PHONE, code: str = CODE) -> OtpSms:
    return OtpSms(phone=phone, code=code, purpose=purpose, sms_code_id=42, idempotency_key=KEY)


def adapter(script: Script, **overrides: object) -> LiveDevsms:
    options: dict[str, object] = {
        "base_url": "https://devsms.uz/api/v1/",
        "token": TOKEN,
        "service_name": "GlowDerma",
        "timeout_seconds": 5,
        "transport": script.transport,
        **overrides,
    }
    return LiveDevsms(**options)  # type: ignore[arg-type]


def sent_body(script: Script) -> dict[str, object]:
    [request] = script.to("POST", SEND)
    body: dict[str, object] = json.loads(request.content)
    return body


# --- the request -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("purpose", "template"),
    [
        (OtpPurpose.ACTION_CONFIRMATION, 1),
        (OtpPurpose.PASSWORD_RESET, 2),
        (OtpPurpose.REGISTRATION, 3),
        (OtpPurpose.LOGIN, 4),
    ],
)
def test_each_purpose_goes_out_with_its_universal_template(purpose: OtpPurpose, template: int) -> None:
    script = Script().on("POST", SEND, reply("devsms", "send_ok"))
    delivery = adapter(script).send_otp(sms(purpose))
    assert delivery == SmsDelivery(message_id="81234", status="sent")
    assert sent_body(script) == {
        "phone": "998901231231",
        "otp": {"template_type": template, "service_name": "GlowDerma", "code": CODE},
    }
    [request] = script.to("POST", SEND)
    assert str(request.url) == "https://devsms.uz/api/v1/sms"
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert request.headers["Idempotency-Key"] == KEY
    assert request.headers["Content-Type"] == "application/json"


def test_the_template_mapping_covers_every_purpose_and_refuses_anything_else() -> None:
    assert {template_type(purpose) for purpose in OtpPurpose} == {1, 2, 3, 4}
    with pytest.raises(SmsRejected) as refusal:
        template_type("marketing")  # type: ignore[arg-type]
    assert refusal.value.reason == "unsupported_purpose"


def test_an_unsupported_purpose_sends_nothing() -> None:
    script = Script()
    with pytest.raises(SmsRejected, match="no universal OTP template"):
        adapter(script).send_otp(sms("order_shipped"))  # type: ignore[arg-type]
    assert script.requests == []


@pytest.mark.parametrize(
    ("phone", "expected"),
    [
        ("+998901231231", "998901231231"),
        ("998901231231", "998901231231"),
        ("+998 90 123 12 31", None),
        ("+99890123123", None),
        ("+7901231231234", None),
        ("+99890123123a", None),
        ("", None),
    ],
)
def test_phone_numbers_are_normalised_to_the_twelve_digit_uzbek_form(
    phone: str, expected: str | None
) -> None:
    assert provider_phone(phone) == expected


def test_a_phone_the_provider_cannot_take_is_refused_before_sending() -> None:
    script = Script()
    with pytest.raises(SmsRejected) as refusal:
        adapter(script).send_otp(sms(phone="+79161234567"))
    assert refusal.value.reason == "invalid_phone" and script.requests == []


@pytest.mark.parametrize("code", ["123", "123456789", "12a456", ""])
def test_a_code_outside_four_to_eight_digits_is_refused_before_sending(code: str) -> None:
    script = Script()
    with pytest.raises(SmsRejected) as refusal:
        adapter(script).send_otp(sms(code=code))
    assert refusal.value.reason == "invalid_code" and script.requests == []


@pytest.mark.parametrize(
    ("name", "valid"),
    [("GlowDerma", True), ("Glow Derma", True), ("Glow-Derma.uz", True), ("G", False), ("Glow_Derma", False)],
)
def test_the_service_name_rule(name: str, valid: bool) -> None:
    assert valid_service_name(name) is valid
    assert adapter(Script(), service_name=name).is_configured() is valid


def test_an_empty_token_is_not_configured() -> None:
    assert adapter(Script(), token=" ").is_configured() is False


def test_the_registry_builds_the_live_adapter_from_the_settings() -> None:
    with override_settings(
        DEVSMS_MODE="live",
        DEVSMS_BASE_URL="https://devsms.uz/api/v1",
        DEVSMS_API_TOKEN=TOKEN,
        DEVSMS_SERVICE_NAME="GlowDerma",
        DEVSMS_TIMEOUT_SECONDS=7,
    ):
        port = get_sms_port()
    assert isinstance(port, LiveDevsms)
    assert (port.base_url, port.token, port.service_name, port.timeout_seconds) == (
        "https://devsms.uz/api/v1",
        TOKEN,
        "GlowDerma",
        7.0,
    )


# --- the outcomes ------------------------------------------------------------------------------------------


def test_a_success_without_an_id_is_repeatable() -> None:
    script = Script().on(
        "POST", SEND, httpx.Response(200, json={"success": True, "data": {"status": "sent"}})
    )
    with pytest.raises(SmsUnavailable) as caught:
        adapter(script).send_otp(sms())
    assert caught.value.sent is False


def test_an_invalid_json_answer_is_repeatable() -> None:
    script = Script().on("POST", SEND, httpx.Response(200, content=b"<html>gateway</html>"))
    with pytest.raises(SmsUnavailable) as caught:
        adapter(script).send_otp(sms())
    assert caught.value.sent is False


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (reply("devsms", "insufficient_balance", 402), "insufficient_balance"),
        (reply("devsms", "moderation_rejected", 403), "moderation_rejected"),
        (reply("devsms", "otp_suspended", 403), "otp_suspended"),
        (reply("devsms", "validation_failed", 422), "validation_failed"),
        (reply("devsms", "provider_rejected", 422), "provider_rejected"),
        (
            httpx.Response(422, json={"success": False, "error_code": "international_disabled"}),
            "invalid_phone",
        ),
        (httpx.Response(400, json={"success": False, "error_code": "bad_request"}), "bad_request"),
        (httpx.Response(404, json={"message": "Not Found"}), "http_404"),
        (
            httpx.Response(422, json={"success": False, "error_code": "idempotency_key_reused"}),
            "idempotency_key_reused",
        ),
    ],
)
def test_definitive_refusals_are_rejections_with_their_reason(answer: httpx.Response, reason: str) -> None:
    script = Script().on("POST", SEND, answer)
    with pytest.raises(SmsRejected) as refusal:
        adapter(script).send_otp(sms())
    assert refusal.value.reason == reason
    assert len(script.to("POST", SEND)) == 1  # never retried by the adapter


@pytest.mark.parametrize(
    "answer",
    [
        connect_error,
        read_timeout,
        reply("devsms", "unauthenticated", 401),
        httpx.Response(401, json={"success": False, "error_code": "token_expired"}),
        httpx.Response(403, json={"success": False, "error_code": "account_blocked"}),
        httpx.Response(403, json={"success": False, "error_code": "token_locked_by_plan"}),
        reply("devsms", "rate_limited", 429),
        reply("devsms", "in_progress", 409),
        httpx.Response(500, json={"success": False, "error_code": "server_error"}),
        httpx.Response(503, json={"success": False, "error_code": "demo_mode"}),
        httpx.Response(302, headers={"Location": "https://devsms.uz/login"}),
    ],
)
def test_outcomes_safe_to_repeat_are_unavailable_not_sent(
    answer: httpx.Response | Callable[[httpx.Request], httpx.Response],
) -> None:
    """With the code's Idempotency-Key a repeat sends and charges at most once, so the handler un-claims."""
    script = Script().on("POST", SEND, answer)
    with pytest.raises(SmsUnavailable) as caught:
        adapter(script, timeout_seconds=1).send_otp(sms())
    assert caught.value.sent is False


def test_provider_outcome_unknown_keeps_the_charged_id_and_is_not_repeated() -> None:
    script = Script().on("POST", SEND, reply("devsms", "outcome_unknown", 502))
    with pytest.raises(SmsUnavailable) as caught:
        adapter(script).send_otp(sms())
    assert caught.value.sent is True and caught.value.message_id == "81235"


def test_operators_learn_why_from_error_lines(caplog: pytest.LogCaptureFixture) -> None:
    for name, status, line in (
        ("unauthenticated", 401, "devsms.auth_failed status=401"),
        ("insufficient_balance", 402, "devsms.insufficient_balance"),
        ("moderation_rejected", 403, "devsms.service_name_rejected remaining_attempts=17"),
        ("otp_suspended", 403, "devsms.otp_suspended blocked_until=2026-11-25 10:00:00"),
    ):
        script = Script().on("POST", SEND, reply("devsms", name, status))
        with caplog.at_level(logging.ERROR, logger=devsms_live.__name__), pytest.raises(Exception):  # noqa: B017, PT011
            adapter(script).send_otp(sms())
        assert any(line in record.getMessage() for record in caplog.records), line


@pytest.mark.parametrize(
    "answer",
    [
        reply("devsms", "send_ok"),
        reply("devsms", "validation_failed", 422),
        reply("devsms", "outcome_unknown", 502),
        reply("devsms", "unauthenticated", 401),
        reply("devsms", "moderation_rejected", 403),
        read_timeout,
    ],
)
def test_no_code_token_or_number_in_a_log_line_or_an_exception(
    answer: httpx.Response | Callable[[httpx.Request], httpx.Response], caplog: pytest.LogCaptureFixture
) -> None:
    script = Script().on("POST", SEND, answer)
    text = ""
    with caplog.at_level(logging.DEBUG):
        try:
            adapter(script, timeout_seconds=1).send_otp(sms())
        except (SmsRejected, SmsUnavailable) as exc:
            text = str(exc)
    logged = " ".join(record.getMessage() for record in caplog.records) + text
    for secret in (CODE, TOKEN, PHONE, PHONE[1:], "901231231"):
        assert secret not in logged
