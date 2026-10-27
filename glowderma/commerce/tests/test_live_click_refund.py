"""The refund state machine over the **live** Click adapter (customer contract §2.13.0): the handler's status
check before any reversal, the double-check after a refusal or an unknown outcome, and the fresh status check
of a staff resolve — all against recorded Merchant API answers (no network)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

import httpx
import pytest
from celery.exceptions import SoftTimeLimitExceeded  # type: ignore[import-untyped]
from django.test import override_settings
from rest_framework.test import APIClient

from glowderma.commerce import services, tasks
from glowderma.commerce.models import Order, OrderEvent
from glowderma.commerce.tests.support import AT, admin, deliver, pay, place
from glowderma.core.models import OutboxMessage
from glowderma.core.seeding import pinned_clock
from glowderma.integrations import httpclient
from glowderma.integrations.click.live import LiveClick
from glowderma.integrations.click.ports import ClickNotProcessed
from glowderma.integrations.tests.live_fixtures import Script, read_timeout, reply
from glowderma.payments.models import Payment, Refund

pytestmark = pytest.mark.django_db
LIVE_CLICK = {
    "CLICK_MODE": "live",
    "CLICK_SERVICE_ID": "36123",
    "CLICK_MERCHANT_ID": "20456",
    "CLICK_MERCHANT_USER_ID": "40789",
    "CLICK_MERCHANT_API_URL": "https://api.click.uz/v2/merchant",
}


@pytest.fixture
def script(monkeypatch: pytest.MonkeyPatch) -> Iterator[Script]:
    """Every live adapter call goes to this scripted Click (never the network)."""
    scripted = Script()
    real = httpclient.new_client
    monkeypatch.setattr(httpclient, "new_client", lambda transport=None: real(scripted.transport))
    monkeypatch.setattr(httpclient, "RETRY_BACKOFF_SECONDS", 0)
    with override_settings(**LIVE_CLICK):
        yield scripted


def cancelled() -> tuple[Order, Refund, str]:
    order = place(fulfilment="pickup", provider="click")
    pay(order)
    with pinned_clock(AT + timedelta(minutes=30)):
        services.cancel_order(order.pk, "Mijoz so‘rovi bilan", admin().pk)
    paydoc = str(Payment.objects.get(order=order).provider_state["click_paydoc_id"])
    return order, Refund.objects.get(payment__order=order), paydoc


def paths(paydoc: str) -> tuple[str, str]:
    return (
        f"/v2/merchant/payment/status/36123/{paydoc}",
        f"/v2/merchant/payment/reversal/36123/{paydoc}",
    )


def test_a_reversal_after_a_status_check_succeeds(script: Script) -> None:
    order, refund, paydoc = cancelled()
    status, reversal = paths(paydoc)
    script.on("GET", status, reply("click", "status_paid")).on(
        "DELETE", reversal, reply("click", "reversal_ok")
    )
    deliver("refund.submit", order.pk)
    refund.refresh_from_db()
    assert refund.state == "succeeded" and refund.provider_refund_id == "5500010248"
    assert [(r.method, r.url.path) for r in script.requests] == [("GET", status), ("DELETE", reversal)]
    assert OrderEvent.objects.filter(order=order, type="refund_succeeded").exists()


def test_a_redelivery_finds_the_payment_already_reversed(script: Script) -> None:
    order, refund, paydoc = cancelled()
    status, reversal = paths(paydoc)
    script.on("GET", status, reply("click", "status_reversed"))
    deliver("refund.submit", order.pk)
    refund.refresh_from_db()
    assert refund.state == "succeeded"
    assert script.to("DELETE", reversal) == []


def test_a_refused_reversal_ends_failed_with_clicks_code(script: Script) -> None:
    order, refund, paydoc = cancelled()
    status, reversal = paths(paydoc)
    script.on("GET", status, reply("click", "status_paid"))
    script.on("DELETE", reversal, reply("click", "reversal_refused"))
    deliver("refund.submit", order.pk)
    refund.refresh_from_db()
    assert refund.state == "failed" and refund.error_code == "-5014"
    assert "998901234567" not in refund.last_error
    assert len(script.to("GET", status)) == 2  # before the reversal and after the refusal


def test_an_unknown_outcome_is_settled_by_the_status_check(script: Script) -> None:
    order, refund, paydoc = cancelled()
    status, reversal = paths(paydoc)
    script.on("GET", status, reply("click", "status_paid"), reply("click", "status_reversed"))
    script.on("DELETE", reversal, read_timeout)
    deliver("refund.submit", order.pk)
    refund.refresh_from_db()
    assert refund.state == "succeeded"
    assert len(script.to("DELETE", reversal)) == 1  # never sent twice


def test_an_unverified_status_after_an_unknown_outcome_alerts_staff(script: Script) -> None:
    order, refund, paydoc = cancelled()
    status, reversal = paths(paydoc)
    script.on("GET", status, reply("click", "status_paid"), reply("click", "status_unknown_code"))
    script.on("DELETE", reversal, httpx.Response(502, json={}))
    deliver("refund.submit", order.pk)
    refund.refresh_from_db()
    assert refund.state == "unknown"  # never retried automatically (spec: refund state machine)


def test_click_down_before_anything_was_sent_keeps_the_refund_requested(script: Script) -> None:
    order, refund, paydoc = cancelled()
    status, _ = paths(paydoc)
    script.on("GET", status, httpx.Response(503, json={}))
    with pytest.raises(Exception, match="HTTP 503"):
        deliver("refund.submit", order.pk)  # the outbox retries the message
    refund.refresh_from_db()
    assert refund.state == "requested"


def test_a_staff_resolve_asks_click_first(script: Script, staff_client: APIClient) -> None:
    order, refund, paydoc = cancelled()
    status, reversal = paths(paydoc)
    script.on("GET", status, reply("click", "status_paid"))
    script.on("DELETE", reversal, reply("click", "reversal_refused"))
    deliver("refund.submit", order.pk)
    url = f"/api/v1/admin/payments/{refund.payment_id}/refunds/{refund.pk}/resolve/"
    refused = staff_client.post(url, {"state": "succeeded"}, format="json")
    assert refused.status_code == 409 and refused.json()["message"] == services.MESSAGE_REFUND_STILL_PAID
    script.on("GET", status, reply("click", "status_unknown_code"))
    unknown = staff_client.post(url, {"state": "succeeded", "manual_payout": True}, format="json")
    assert unknown.status_code == 503 and unknown.json()["code"] == "provider_unavailable"
    script.on("GET", status, reply("click", "status_reversed"))
    assert staff_client.post(url, {"state": "succeeded"}, format="json").status_code == 200
    refund.refresh_from_db()
    assert refund.state == "succeeded"


def garbled(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=httpx.ByteStream(b"not gzip"))


def soft_limit(request: httpx.Request) -> httpx.Response:
    raise SoftTimeLimitExceeded()


def message_of(order: Order) -> OutboxMessage:
    return OutboxMessage.objects.get(topic="refund.submit", payload__order_id=order.pk)


@pytest.mark.parametrize("redelivery_after", [timedelta(seconds=4), timedelta(minutes=10)])
def test_an_undecodable_reversal_answer_is_unknown_and_never_reversed_again(
    script: Script, redelivery_after: timedelta
) -> None:
    """A-M1 / R1: a corrupt answer after the claim is an unknown outcome — never ``submitting`` until the
    lease runs out, never a second automatic reversal (the quick and the after-lease redelivery alike)."""
    order, refund, paydoc = cancelled()
    status, reversal = paths(paydoc)
    script.on("GET", status, reply("click", "status_paid"))
    script.on("DELETE", reversal, garbled, reply("click", "reversal_ok"))
    with pinned_clock(AT + timedelta(minutes=31)):
        tasks.submit_refund(message_of(order).payload)
    refund.refresh_from_db()
    assert refund.state == "unknown" and "DecodingError" in refund.last_error
    with pinned_clock(AT + timedelta(minutes=31) + redelivery_after):
        tasks.submit_refund(message_of(order).payload)
    refund.refresh_from_db()
    assert refund.state == "unknown"
    assert len(script.to("DELETE", reversal)) == 1
    assert OrderEvent.objects.filter(order=order, type="refund_failed", data__state="unknown").exists()


@pytest.mark.parametrize("failure", ["soft_time_limit", "untyped"])
def test_any_other_exception_after_the_claim_records_unknown(
    script: Script, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """R1 / R8: a Celery soft time limit (or a bug) during the reversal: ``unknown`` at once and re-raised;
    the redelivery 2 s later finds ``unknown`` and sends nothing."""
    order, refund, paydoc = cancelled()
    status, reversal = paths(paydoc)
    script.on("GET", status, reply("click", "status_paid"))
    expected: type[BaseException]
    if failure == "soft_time_limit":
        script.on("DELETE", reversal, soft_limit)
        expected = SoftTimeLimitExceeded
    else:

        def broken(self: LiveClick, payment_ref: str, amount: int) -> None:
            raise KeyError("an adapter bug")

        monkeypatch.setattr(LiveClick, "refund", broken)
        expected = KeyError
    with pytest.raises(expected):
        tasks.handle_refund_submit(message_of(order))
    refund.refresh_from_db()
    assert refund.state == "unknown" and "outcome unknown" in refund.last_error
    tasks.handle_refund_submit(message_of(order))
    refund.refresh_from_db()
    assert refund.state == "unknown"
    assert len(script.to("DELETE", reversal)) <= 1


@pytest.mark.parametrize("status_code", [302, 408, 429, 401, 403])
def test_a_reversal_click_did_not_process_releases_the_claim_and_retries(
    script: Script, status_code: int
) -> None:
    """R5 / A-S6: 3xx, 408, 425, 429 and auth refusals are "not processed": the refund goes back to
    ``requested`` (never ``failed``), the outbox retries, and the retry reverses after a fresh status
    check."""
    order, refund, paydoc = cancelled()
    status, reversal = paths(paydoc)
    script.on("GET", status, reply("click", "status_paid"))
    script.on("DELETE", reversal, httpx.Response(status_code, json={}), reply("click", "reversal_ok"))
    with pytest.raises(ClickNotProcessed):
        tasks.handle_refund_submit(message_of(order))
    refund.refresh_from_db()
    assert refund.state == "requested" and f"HTTP {status_code}" in refund.last_error
    tasks.handle_refund_submit(message_of(order))
    refund.refresh_from_db()
    assert refund.state == "succeeded"
    assert len(script.to("GET", status)) == 2 and len(script.to("DELETE", reversal)) == 2


def test_a_released_claim_that_dies_ends_failed_dead_letter(script: Script) -> None:
    """A refund whose reversals Click never processed dies ``requested`` → ``failed`` ``dead_letter``."""
    order, refund, paydoc = cancelled()
    status, reversal = paths(paydoc)
    script.on("GET", status, reply("click", "status_paid"))
    script.on("DELETE", reversal, httpx.Response(429, json={}))
    with pytest.raises(ClickNotProcessed):
        tasks.handle_refund_submit(message_of(order))
    tasks.on_refund_submit_dead(message_of(order))
    refund.refresh_from_db()
    assert refund.state == "failed" and refund.error_code == "dead_letter"
