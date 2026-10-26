"""The refund state machine over the **live** Click adapter (customer contract §2.13.0): the handler's status
check before any reversal, the double-check after a refusal or an unknown outcome, and the fresh status check
of a staff resolve — all against recorded Merchant API answers (no network)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

import httpx
import pytest
from django.test import override_settings
from rest_framework.test import APIClient

from glowderma.commerce import services
from glowderma.commerce.models import Order, OrderEvent
from glowderma.commerce.tests.support import AT, admin, deliver, pay, place
from glowderma.core.seeding import pinned_clock
from glowderma.integrations import httpclient
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
