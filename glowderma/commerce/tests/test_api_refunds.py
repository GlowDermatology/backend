"""Admin API — the refund operations of the payments screen (contract §7 item 18): retry a ``failed`` refund,
resolve an ``unknown`` / ``manual_required`` one; both write the order events and the audit trail."""

import uuid
from datetime import timedelta
from typing import Any
from unittest import mock

import pytest
from rest_framework.test import APIClient

from glowderma.commerce import services, topics
from glowderma.commerce.models import Order, OrderEvent
from glowderma.commerce.tests.support import AT, admin, deliver, pay, place
from glowderma.core.models import AuditEntry, OutboxMessage
from glowderma.core.seeding import pinned_clock
from glowderma.loyalty import services as loyalty_services
from glowderma.loyalty.services import PurchaseConflict
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
    assert audit.actor_id is not None and audit.after["to"] == "succeeded"  # type: ignore[index]
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
    assert event.data == {
        "refund_id": new.pk,
        "amount": new.amount,
        "provider": "click",
        "manual": False,
        "manual_reason": None,
    }
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


def test_retry_of_a_refund_whose_payment_was_refunded_since_is_refused_in_uzbek(
    staff_client: APIClient,
) -> None:
    """A failed refund, retried successfully: retrying the old failed row again must answer the contract's
    Uzbek message, never payments' internal "Payment N is refunded, not paid"."""
    order, refund = _cancelled("click", refund="failed")
    deliver(topics.REFUND_SUBMIT, order.pk)
    assert Refund.objects.get(pk=refund.pk).state == "failed"
    assert staff_client.post(_url(refund, "retry"), {}, format="json").status_code == 200
    Order.objects.filter(pk=order.pk).update(metadata={})  # Click accepts the reversal this time
    deliver(topics.REFUND_SUBMIT, order.pk)
    assert Payment.objects.get(order=order).status == "refunded"
    again = staff_client.post(_url(refund, "retry"), {}, format="json")
    assert again.status_code == 409
    assert again.json()["code"] == "invalid_transition"
    assert again.json()["message"] == "Bu qaytarishni qayta yuborib bo‘lmaydi."


def test_resolve_of_a_closed_refund_is_refused_in_uzbek(staff_client: APIClient) -> None:
    order, refund = _cancelled("click")
    deliver(topics.REFUND_SUBMIT, order.pk)
    assert Refund.objects.get(pk=refund.pk).state == "succeeded"
    for state in ("failed", "succeeded"):
        response = staff_client.post(_url(refund, "resolve"), {"state": state}, format="json")
        assert response.status_code == 409
        assert response.json()["message"] == "Bu qaytarishni yakunlab bo‘lmaydi."


def test_a_loyalty_purchase_conflict_is_answered_in_uzbek(staff_client: APIClient) -> None:
    order = pay(place(fulfilment="pickup"))
    conflict = PurchaseConflict(f"Order {order.pk} already belongs to another customer's purchase")
    with mock.patch.object(services, "transition", side_effect=conflict):
        response = staff_client.post(
            f"/api/v1/admin/orders/{order.pk}/transition/",
            {"status": "delivered"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
    assert response.status_code == 409
    assert response.json()["code"] == "conflict"
    assert response.json()["message"] == services.MESSAGE_PURCHASE_CONFLICT
    assert "already belongs" not in response.content.decode()


def test_retry_is_refused_while_a_later_retry_is_unknown(staff_client: APIClient) -> None:
    """Contract §2.12: an ``unknown`` refund counts as open (``OPEN_REFUND_STATES``), so retrying the old
    failed refund again answers 409 ``conflict``, not a second refund of the still-``paid`` payment."""
    order, refund = _cancelled("click", refund="failed")
    deliver(topics.REFUND_SUBMIT, order.pk)
    assert staff_client.post(_url(refund, "retry"), {}, format="json").status_code == 200
    Order.objects.filter(pk=order.pk).update(metadata={"fake": {"click": {"refund": "unknown"}}})
    deliver(topics.REFUND_SUBMIT, order.pk)
    later = Refund.objects.exclude(pk=refund.pk).get()
    assert later.state == "unknown" and Payment.objects.get(order=order).status == "paid"
    response = staff_client.post(_url(refund, "retry"), {}, format="json")
    assert response.status_code == 409
    assert response.json()["code"] == "conflict"
    assert response.json()["message"] == "Bu to‘lov uchun qaytarish allaqachon jarayonda."
    assert Refund.objects.filter(payment__order=order).count() == 2


def test_the_refund_operations_never_touch_the_loyalty_purchase(staff_client: APIClient) -> None:
    """Contract §2.2/§2.12: the purchase-conflict outcome belongs to cancel/transition/retry-shipment only;
    retry and resolve neither record nor reverse a loyalty purchase."""
    order, refund = _cancelled("click", refund="failed")
    deliver(topics.REFUND_SUBMIT, order.pk)
    _, payme = _cancelled("payme")
    conflict = PurchaseConflict("never raised by the refund operations")
    with (
        mock.patch.object(loyalty_services, "record_online_purchase", side_effect=conflict),
        mock.patch.object(loyalty_services, "reverse_purchase", side_effect=conflict),
    ):
        retried = staff_client.post(_url(refund, "retry"), {}, format="json")
        assert retried.status_code == 200, retried.json()
        resolved = staff_client.post(_url(payme, "resolve"), {"state": "succeeded"}, format="json")
        assert resolved.status_code == 200, resolved.json()
