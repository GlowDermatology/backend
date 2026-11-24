"""``sms.send`` over the **live** DevSMS adapter (customer contract §2.14.1), against answers shaped as DevSMS
API v1 documents them (no network): the id is stored, a repeatable failure un-claims and the retry carries the
same Idempotency-Key, DevSMS's "outcome unknown" keeps the claim (never resent), a refusal is recorded
without the number or the code."""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from django.core.cache import cache
from django.test import override_settings

from glowderma.accounts import services, tasks
from glowderma.accounts.models import Customer, SmsCode
from glowderma.accounts.tasks import otp_delivery
from glowderma.core.models import OutboxMessage
from glowderma.integrations import httpclient
from glowderma.integrations.devsms.ports import SmsUnavailable
from glowderma.integrations.models import IntegrationState
from glowderma.integrations.tests.live_fixtures import Script, read_timeout, reply

pytestmark = pytest.mark.django_db
PHONE = "+998901231231"
SEND = "/api/v1/sms"
LIVE_DEVSMS = {
    "DEVSMS_MODE": "live",
    "DEVSMS_BASE_URL": "https://devsms.uz/api/v1",
    "DEVSMS_API_TOKEN": "devsms-api-token-value",  # noqa: S106 - a test value
    "DEVSMS_SERVICE_NAME": "GlowDerma",
}


@pytest.fixture
def script(monkeypatch: pytest.MonkeyPatch) -> Iterator[Script]:
    scripted = Script()
    real = httpclient.new_client
    monkeypatch.setattr(httpclient, "new_client", lambda transport=None: real(scripted.transport))
    monkeypatch.setattr(httpclient, "RETRY_BACKOFF_SECONDS", 0)
    yield scripted


def issue(purpose: str = "register") -> tuple[OutboxMessage, SmsCode]:
    if purpose == "reset_password":
        Customer.objects.create(phone=PHONE, password="x")  # noqa: S106 - a test value
    services.send_code(PHONE, purpose, ip="203.0.113.1", fid="fid-live-0001", lang="uz", platform="ios")
    row = SmsCode.objects.get(phone=PHONE)
    return OutboxMessage.objects.get(dedupe_key=f"sms.send:{row.pk}"), row


@pytest.mark.parametrize(("purpose", "template"), [("register", 3), ("reset_password", 2)])
def test_an_accepted_code_stores_the_devsms_id(script: Script, purpose: str, template: int) -> None:
    message, row = issue(purpose)
    script.on("POST", SEND, reply("devsms", "send_ok"))
    with override_settings(**LIVE_DEVSMS):
        tasks.send_sms(message)
        tasks.send_sms(message)  # a redelivery never sends twice
    row.refresh_from_db()
    assert row.provider_message_id == "81234" and row.sent_at is not None
    [request] = script.to("POST", SEND)
    body = json.loads(request.content)
    assert body["phone"] == PHONE[1:] and body["otp"]["template_type"] == template
    assert (
        body["otp"]["service_name"] == "GlowDerma"
        and services.code_hmac(body["otp"]["code"]) == row.code_hmac
    )
    assert "from" not in body and "message" not in body  # the shared default sender, the approved text
    assert request.headers["Idempotency-Key"] == otp_delivery.sms_idempotency_key(row)
    assert cache.get(services.otp_payload_key(row.pk)) is None


def test_a_timeout_unclaims_and_the_retry_repeats_the_same_key(script: Script) -> None:
    message, row = issue()
    script.on("POST", SEND, read_timeout, reply("devsms", "send_ok"))
    with override_settings(**LIVE_DEVSMS):
        with pytest.raises(SmsUnavailable):
            tasks.send_sms(message)  # the outbox retries this delivery
        row.refresh_from_db()
        assert row.sent_at is None and cache.get(services.otp_payload_key(row.pk)) is not None
        tasks.send_sms(message)
    row.refresh_from_db()
    first, second = script.to("POST", SEND)
    assert first.headers["Idempotency-Key"] == second.headers["Idempotency-Key"]
    assert json.loads(first.content) == json.loads(second.content)  # the same code: never regenerated
    assert row.provider_message_id == "81234" and row.sent_at is not None


def test_an_unknown_outcome_keeps_the_claim_and_is_never_resent(script: Script) -> None:
    message, row = issue()
    script.on("POST", SEND, reply("devsms", "outcome_unknown", 502))
    with override_settings(**LIVE_DEVSMS):
        tasks.send_sms(message)  # the user may have the code; they resend after 60 s if not
        tasks.send_sms(message)
    row.refresh_from_db()
    assert row.sent_at is not None and row.provider_message_id == "81235"
    assert len(script.to("POST", SEND)) == 1


@pytest.mark.parametrize(
    ("answer", "status"),
    [("validation_failed", 422), ("moderation_rejected", 403), ("insufficient_balance", 402)],
)
def test_a_refusal_is_recorded_without_the_number_or_the_code(
    script: Script, answer: str, status: int
) -> None:
    message, row = issue()
    script.on("POST", SEND, reply("devsms", answer, status))
    with override_settings(**LIVE_DEVSMS):
        tasks.send_sms(message)
    state = IntegrationState.objects.get(provider="devsms")
    assert state.consecutive_failures == 1 and state.last_error.startswith("rejected")
    assert PHONE[1:] not in state.last_error
    row.refresh_from_db()
    assert row.sent_at is not None  # final: never retried
