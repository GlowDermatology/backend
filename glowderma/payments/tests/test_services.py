"""payments.services / selectors (design 02 §2.1–§2.2): payment states, the refund state machine, the list
selector and the dashboard revenue (net of succeeded refunds, attributed to the payment's bucket)."""

from datetime import datetime, timedelta

import pytest

from glowderma.core.api.exceptions import Conflict, InvalidTransition
from glowderma.core.models import AuditEntry, OutboxMessage
from glowderma.core.timeutils import TASHKENT
from glowderma.payments import selectors, services
from glowderma.payments.tests.factories import OrderRowFactory, PaymentFactory, RefundFactory

pytestmark = pytest.mark.django_db
AT = datetime(2026, 10, 23, 14, 5, tzinfo=TASHKENT)


def test_payment_lifecycle() -> None:
    order = OrderRowFactory.create()
    created = services.create_payment(order.pk, "click", order.total)
    assert (created.status, created.amount) == ("pending", order.total)
    with pytest.raises(Conflict):
        services.create_payment(order.pk, "payme", order.total)
    with pytest.raises(ValueError):
        services.create_payment(order.pk, "cash", 1)
    with pytest.raises(ValueError):
        services.create_payment(order.pk, "click", 0)
    paid = services.confirm_payment(created.payment_id, "CLK-1", AT, raw={"click_trans_id": "1"})
    assert (paid.status, paid.provider_txn_id, paid.confirmed_at, paid.changed) == ("paid", "CLK-1", AT, True)
    assert not services.confirm_payment(created.payment_id, "CLK-1", AT).changed
    with pytest.raises(InvalidTransition):
        services.confirm_payment(created.payment_id, "CLK-2", AT)
    with pytest.raises(ValueError):
        services.confirm_payment(created.payment_id, "", AT)
    with pytest.raises(InvalidTransition):
        services.fail_payment(created.payment_id, "cancelled")
    with pytest.raises(services.PaymentNotFound):
        services.fail_payment(10**9, "x")
    second = services.create_payment(OrderRowFactory.create().pk, "payme", 1000)
    failed = services.fail_payment(second.payment_id, "expired", AT)
    assert (failed.status, failed.changed) == ("failed", True)
    assert not services.fail_payment(second.payment_id, "expired").changed
    with pytest.raises(ValueError):
        services.confirm_payment(created.payment_id, "CLK-1", AT.replace(tzinfo=None))


def test_payme_state_one_is_left_pending_by_unilateral_fails() -> None:
    payment = PaymentFactory.create(pending=True, provider="payme", provider_state={"state": 1})
    assert not services.fail_payment(payment.pk, "cancelled", unilateral=True).changed
    assert services.fail_payment(payment.pk, "-31008").changed  # Payme's own CancelTransaction
    assert services.can_fail_unilaterally("payme", {"state": 2})


def test_click_refund_state_machine_and_audits() -> None:
    payment = PaymentFactory.create()
    staff = payment.order.cancelled_by
    requested = services.create_refund(payment.pk, payment.amount, None, at=AT)
    assert (requested.state, requested.changed) == ("requested", True)
    assert (
        OutboxMessage.objects.get(topic="refund.submit").dedupe_key == f"refund.submit:{requested.refund_id}"
    )
    again = services.create_refund(payment.pk, payment.amount, staff.pk if staff else None)
    assert (again.refund_id, again.changed) == (requested.refund_id, False)
    submitting = services.mark_submitting(requested.refund_id, AT)
    assert (submitting.state, submitting.attempts) == ("submitting", 1)
    assert services.mark_submitting(requested.refund_id, AT).attempts == 2  # a redelivery resubmits
    unknown = services.mark_unknown(requested.refund_id, "timeout", AT)
    assert unknown.state == "unknown" and not services.mark_unknown(requested.refund_id).changed
    succeeded = services.mark_succeeded(
        requested.refund_id, AT + timedelta(hours=1), provider_refund_id="R-1"
    )
    assert (succeeded.state, succeeded.payment_refunded) == ("succeeded", True)
    assert not services.mark_succeeded(requested.refund_id).changed
    payment.refresh_from_db()
    assert (payment.status, payment.refunded_at) == ("refunded", AT + timedelta(hours=1))
    with pytest.raises(InvalidTransition):
        services.mark_failed(requested.refund_id, "late")
    other = PaymentFactory.create()
    with pytest.raises(InvalidTransition):
        services.mark_unknown(services.create_refund(other.pk, other.amount, None).refund_id)
    transitions = [
        a["to"]  # type: ignore[index]
        for a in AuditEntry.objects.filter(action="refund.state_changed").values_list("after", flat=True)
    ]
    assert transitions == ["submitting", "submitting", "unknown", "succeeded"]
    assert AuditEntry.objects.filter(action="refund.requested").count() == 2


def test_payme_refund_is_manual_and_resolvable() -> None:
    payment = PaymentFactory.create(provider="payme")
    refund = services.create_refund(payment.pk, payment.amount, None)
    assert refund.state == "manual_required"
    assert not OutboxMessage.objects.filter(topic="refund.submit").exists()
    with pytest.raises(InvalidTransition):
        services.mark_submitting(refund.refund_id)
    failed = services.mark_failed(refund.refund_id, "resolved_failed", actor_id=None)
    assert failed.state == "failed" and not services.mark_failed(refund.refund_id, "again").changed
    retried = services.retry_refund(refund.refund_id, None)
    assert retried.state == "manual_required" and retried.refund_id != refund.refund_id
    with pytest.raises(Conflict):
        services.retry_refund(refund.refund_id, None)  # an open refund exists now
    with pytest.raises(InvalidTransition):
        services.retry_refund(retried.refund_id, None)  # not failed
    with pytest.raises(services.RefundNotFound):
        services.retry_refund(10**9, None)


def test_refund_guards() -> None:
    pending = PaymentFactory.create(pending=True)
    with pytest.raises(InvalidTransition):
        services.create_refund(pending.pk, pending.amount, None)
    paid = PaymentFactory.create()
    with pytest.raises(ValueError):
        services.create_refund(paid.pk, 1, None)
    with pytest.raises(ValueError):
        services.create_refund(paid.pk, paid.amount, None, origin="bogus")
    with pytest.raises(InvalidTransition):
        services.retry_refund(RefundFactory.create(succeeded=True).pk, None)


def test_selectors_display_payment_and_refunds() -> None:
    order = OrderRowFactory.create()
    assert selectors.display_payment(order.pk) is None
    failed = PaymentFactory.create(order=order, failed=True)
    assert selectors.display_payment(order.pk).id == failed.pk  # type: ignore[union-attr]
    pending = PaymentFactory.create(order=order, pending=True)
    assert selectors.display_payment(order.pk).id == pending.pk  # type: ignore[union-attr]
    assert selectors.pick_display([failed, pending]) == pending and selectors.pick_display([]) is None
    assert selectors.active_payment(order.pk, "pending").id == pending.pk  # type: ignore[union-attr]
    assert selectors.get_payment(10**9) is None and selectors.get_refund(10**9) is None
    refund = RefundFactory.create(failed=True)
    assert selectors.refunds_needing_action() == 1
    row = selectors.refunds_for_order(refund.payment.order_id)[0]
    assert (row.id, row.state, row.provider) == (refund.pk, "failed", refund.payment.provider)


def test_revenue_buckets_attribute_refunds_to_the_payment_bucket() -> None:
    day = datetime(2026, 10, 24, tzinfo=TASHKENT)
    nine = PaymentFactory.create(amount=300_000, confirmed_at=day.replace(hour=9, minute=10))
    PaymentFactory.create(amount=200_000, confirmed_at=day.replace(hour=9, minute=50))
    PaymentFactory.create(amount=150_000, confirmed_at=day.replace(hour=11))
    PaymentFactory.create(pending=True, amount=999_999)
    RefundFactory.create(payment=nine, succeeded=True, amount=300_000)
    hours = selectors.revenue_buckets(day, day + timedelta(days=1), "hour")
    assert hours == {day.replace(hour=9): 200_000, day.replace(hour=11): 150_000}
    days = selectors.revenue_buckets(day - timedelta(days=1), day + timedelta(days=1), "day")
    assert days == {day: 350_000}
