"""Admin API — the refund operations of the payments screen (contract §7 item 18): retry a ``failed`` refund,
resolve an ``unknown`` / ``manual_required`` one; both write the order events and the audit trail."""

from datetime import timedelta
from typing import Any

import pytest
from rest_framework.test import APIClient

from glowderma.commerce import services, topics
from glowderma.commerce.models import OrderEvent
from glowderma.commerce.tests.support import AT, admin, deliver, pay, place
from glowderma.core.models import AuditEntry, OutboxMessage
from glowderma.core.seeding import pinned_clock
from glowderma.payments.models import Payment, Refund

pytestmark = pytest.mark.django_db


def _cancelled(provider: str, **fake: Any) -> tuple[Any, Refund]:
    order = pay(place(fulfilment="pickup", provider=provider, fake={"click": fake} if fake else None))
    with pinned_clock(AT + timedelta(minutes=60)):
        services.cancel_order(order.pk, "Mijoz so‘rovi bilan", admin().pk)
    return order, Refund.objects.get(payment__order=order)


def _url(refund: Refund, action: str) -> str:
    return f"/api/v1/admin/payments/{refund.payment_id}/refunds/{refund.pk}/{action}/"


def test_resolve_a_manual_payme_refund(staff_client: APIClient) -> None:
    order, refund = _cancelled("payme")
    bad = staff_client.post(_url(refund, "resolve"), {"state": "unknown"}, format="json")
    assert bad.status_code == 400 and bad.json()["errors"][0] == {
        "field": "state",
        "code": "invalid_choice",
        "message": "Ruxsat etilmagan qiymat.",
    }
    response = staff_client.post(_url(refund, "resolve"), {"state": "succeeded"}, format="json")
    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["status"] == "refunded" and body["refunds"][0]["state"] == "succeeded"
    assert OrderEvent.objects.get(order=order, type="refund_succeeded").data["refund_id"] == refund.pk
    audit = AuditEntry.objects.filter(action="refund.state_changed").latest("id")
    assert audit.actor_id is not None and audit.after["to"] == "succeeded"
    again = staff_client.post(_url(refund, "resolve"), {"state": "failed"}, format="json")
    assert again.status_code == 409 and again.json()["message"] == "Bu qaytarishni yakunlab bo‘lmaydi."


def test_resolve_unknown_as_failed_then_retry(staff_client: APIClient) -> None:
    order, refund = _cancelled("click", refund="unknown")
    deliver(topics.REFUND_SUBMIT, order.pk)
    assert Refund.objects.get(pk=refund.pk).state == "unknown"
    early = staff_client.post(_url(refund, "retry"), {}, format="json")
    assert early.status_code == 409 and early.json()["message"] == "Bu qaytarishni qayta yuborib bo‘lmaydi."
    resolved = staff_client.post(_url(refund, "resolve"), {"state": "failed"}, format="json")
    assert resolved.status_code == 200 and resolved.json()["refunds"][0]["state"] == "failed"
    failed_events = OrderEvent.objects.filter(order=order, type="refund_failed")
    assert sorted(failed_events.values_list("data__state", flat=True)) == ["failed", "unknown"]
    retried = staff_client.post(_url(refund, "retry"), {}, format="json")
    assert retried.status_code == 200
    states = [r["state"] for r in retried.json()["refunds"]]
    assert states == ["requested", "failed"]
    new = Refund.objects.exclude(pk=refund.pk).get()
    assert (
        new.origin == "manual" and OutboxMessage.objects.filter(dedupe_key=f"refund.submit:{new.pk}").exists()
    )
    event = OrderEvent.objects.filter(order=order, type="refund_requested").latest("id")
    assert event.data == {"refund_id": new.pk, "amount": new.amount, "provider": "click", "manual": False}
    assert event.source == "admin"
    busy = staff_client.post(_url(new, "resolve"), {"state": "failed"}, format="json")
    assert busy.status_code == 409


def test_refund_routes_404(staff_client: APIClient) -> None:
    _, refund = _cancelled("payme")
    other = Payment.objects.create(
        order=refund.payment.order, provider="click", amount=1, status="failed", failed_at=AT
    )
    assert (
        staff_client.post(
            f"/api/v1/admin/payments/{other.pk}/refunds/{refund.pk}/retry/", {}, format="json"
        ).status_code
        == 404
    )  # noqa: E501
    assert (
        staff_client.post(
            "/api/v1/admin/payments/1/refunds/999999/resolve/", {"state": "failed"}, format="json"
        ).status_code
        == 404
    )  # noqa: E501
