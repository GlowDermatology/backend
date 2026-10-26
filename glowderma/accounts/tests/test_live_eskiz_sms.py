"""``sms.send`` over the **live** Eskiz adapter (customer contract §2.14.1): the id is stored, a connect
failure un-claims for the outbox retry, a timeout keeps the claim (never resent), a refusal is recorded —
against the answers of Eskiz's published collection (no network)."""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from django.core.cache import cache
from django.test import override_settings

from glowderma.accounts import services, tasks
from glowderma.accounts.models import SmsCode
from glowderma.core.models import OutboxMessage
from glowderma.integrations import httpclient
from glowderma.integrations.eskiz.ports import EskizUnavailable
from glowderma.integrations.models import IntegrationState
from glowderma.integrations.tests.live_fixtures import Script, connect_error, read_timeout, reply

pytestmark = pytest.mark.django_db
PHONE = "+998901231231"
LIVE_ESKIZ = {
    "ESKIZ_MODE": "live",
    "ESKIZ_BASE_URL": "https://notify.eskiz.uz/api",
    "ESKIZ_EMAIL": "sms@glowderma.uz",
    "ESKIZ_PASSWORD": "eskiz-password-value",
    "ESKIZ_SENDER": "GlowDerma",
}


@pytest.fixture
def script(monkeypatch: pytest.MonkeyPatch) -> Iterator[Script]:
    scripted = Script().on("POST", "/api/auth/login", reply("eskiz", "login_ok"))
    real = httpclient.new_client
    monkeypatch.setattr(httpclient, "new_client", lambda transport=None: real(scripted.transport))
    monkeypatch.setattr(httpclient, "RETRY_BACKOFF_SECONDS", 0)
    yield scripted


def issue() -> tuple[OutboxMessage, SmsCode]:
    services.send_code(PHONE, "register", ip="203.0.113.1", fid="fid-live-0001", lang="uz", platform="ios")
    row = SmsCode.objects.get(phone=PHONE)
    return OutboxMessage.objects.get(dedupe_key=f"sms.send:{row.pk}"), row


def test_an_accepted_message_stores_the_eskiz_id(script: Script) -> None:
    message, row = issue()
    script.on("POST", "/api/message/sms/send", reply("eskiz", "send_ok"))
    with override_settings(**LIVE_ESKIZ):
        tasks.send_sms(message)
        tasks.send_sms(message)  # a redelivery never sends twice
    row.refresh_from_db()
    assert row.provider_message_id == "59bf10a2-aba8-4694-8fd5-0be20102a580" and row.sent_at is not None
    assert len(script.to("POST", "/api/message/sms/send")) == 1
    assert cache.get(services.otp_payload_key(row.pk)) is None


def test_a_connect_failure_unclaims_for_the_outbox_retry(script: Script) -> None:
    message, row = issue()
    script.on("POST", "/api/message/sms/send", connect_error)
    with override_settings(**LIVE_ESKIZ), pytest.raises(EskizUnavailable):
        tasks.send_sms(message)
    row.refresh_from_db()
    assert row.sent_at is None and cache.get(services.otp_payload_key(row.pk)) is not None


def test_a_timeout_after_sending_keeps_the_claim(script: Script) -> None:
    message, row = issue()
    script.on("POST", "/api/message/sms/send", read_timeout)
    with override_settings(**LIVE_ESKIZ):
        tasks.send_sms(message)  # outcome unknown: never resent, the user resends after 60 s
        tasks.send_sms(message)
    row.refresh_from_db()
    assert row.sent_at is not None and row.provider_message_id == ""
    assert len(script.to("POST", "/api/message/sms/send")) == 1


def test_a_refusal_is_recorded_without_the_number(script: Script) -> None:
    message, _ = issue()
    script.on(
        "POST", "/api/message/sms/send", httpx.Response(400, json={"message": f"Bad number {PHONE[1:]}"})
    )
    with override_settings(**LIVE_ESKIZ):
        tasks.send_sms(message)
    state = IntegrationState.objects.get(provider="eskiz")
    assert state.consecutive_failures == 1 and PHONE[1:] not in state.last_error
