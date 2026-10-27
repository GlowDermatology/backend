"""Click refunds by ``click_paydoc_id``, the manual reasons, the fresh status check and the guard of a failed
Click refund's resolve (customer contract §2.13.0; admin §2.10, §2.12, §4, §7 item 18)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest import mock

import pytest
from django.test import override_settings
from rest_framework.test import APIClient

from glowderma.commerce import services, tasks
from glowderma.commerce.models import Order, OrderEvent
from glowderma.commerce.tests.support import AT, admin, deliver, pay, place
from glowderma.core.api.exceptions import Conflict
from glowderma.core.models import AuditEntry, OutboxMessage
from glowderma.core.seeding import pinned_clock
from glowderma.integrations.click import fake as fake_click
from glowderma.integrations.click.fake import FakeClick
from glowderma.payments import selectors as payment_selectors
from glowderma.payments.models import Payment, Refund
from glowderma.payments.tests.factories import PaymentFactory, RefundFactory

pytestmark = pytest.mark.django_db
PROD_FAKE_CLICK = {"DEV_FEATURES_ALLOWED": False, "CLICK_MODE": "fake"}


def _cancelled(provider: str = "click", *, paydoc: bool = True, **scenario: Any) -> tuple[Order, Refund]:
    order = place(fulfilment="pickup", provider=provider, fake={"click": scenario} if scenario else None)
    pay(order, paydoc=paydoc)
    with pinned_clock(AT + timedelta(minutes=30)):
        services.cancel_order(order.pk, "Mijoz so‘rovi bilan", admin().pk)
    return order, Refund.objects.get(payment__order=order)


def _failed(**scenario: Any) -> tuple[Order, Refund]:
    order, refund = _cancelled(refund="failed", **scenario)
    deliver("refund.submit", order.pk)
    refund.refresh_from_db()
    assert refund.state == "failed"
    return order, refund


def _set_status(order: Order, status: str) -> None:
    order.metadata = {"fake": {"click": {"refund": "failed", "status": status}}}
    order.save(update_fields=["metadata"])


def _resolve_url(refund: Refund) -> str:
    return f"/api/v1/admin/payments/{refund.payment_id}/refunds/{refund.pk}/resolve/"


def test_click_refunds_are_keyed_by_the_paydoc_id() -> None:
    order, refund = _cancelled()
    paydoc = Payment.objects.get(order=order).provider_state["click_paydoc_id"]
    assert payment_selectors.get_refund(refund.pk).provider_payment_ref == paydoc  # type: ignore[union-attr]
    deliver("refund.submit", order.pk)
    assert fake_click.calls == [("payment_status", paydoc), ("refund", paydoc)]
    event = OrderEvent.objects.get(order=order, type="refund_requested")
    assert event.data["manual"] is False and event.data["manual_reason"] is None


def test_a_payment_without_a_paydoc_needs_a_manual_payout(staff_client: APIClient) -> None:
    order, refund = _cancelled(paydoc=False)
    assert refund.state == "manual_required"
    assert not OutboxMessage.objects.filter(topic="refund.submit").exists()
    event = OrderEvent.objects.get(order=order, type="refund_requested")
    assert event.data["manual"] is True and event.data["manual_reason"] == "no_paydoc"
    assert fake_click.calls == []
    assert staff_client.post(_resolve_url(refund), {"state": "succeeded"}, format="json").status_code == 200


def test_payme_refunds_say_payme() -> None:
    order, refund = _cancelled("payme")
    assert refund.state == "manual_required"
    assert OrderEvent.objects.get(order=order, type="refund_requested").data["manual_reason"] == "payme"


def test_a_refund_created_while_click_is_fake_in_production() -> None:
    order = place(fulfilment="pickup")
    pay(order)
    with override_settings(**PROD_FAKE_CLICK), pinned_clock(AT + timedelta(minutes=30)):
        services.cancel_order(order.pk, "Mijoz so‘rovi bilan", admin().pk)
    refund = Refund.objects.get(payment__order=order)
    assert refund.state == "manual_required" and fake_click.calls == []
    event = OrderEvent.objects.get(order=order, type="refund_requested")
    assert (event.data["manual"], event.data["manual_reason"]) == (True, "fake_mode")


def test_submit_rechecks_before_any_port_call() -> None:
    order, refund = _cancelled()
    with override_settings(**PROD_FAKE_CLICK):
        deliver("refund.submit", order.pk)
    refund.refresh_from_db()
    assert refund.state == "manual_required" and fake_click.calls == []
    assert (
        OrderEvent.objects.get(order=order, type="refund_requested").data["manual_reason"] is None
    )  # as written


def test_a_submitting_refund_redelivered_after_a_switch_to_fake_becomes_unknown() -> None:
    """The worker crashed between Click's answer and the commit, then Click was switched to fake: the
    reversal's outcome is unknown, so the refund goes where staff see it, never stays ``submitting``."""
    from glowderma.payments import services as payment_services
    from glowderma.payments.models import ATTENTION_REFUND_STATES

    order, refund = _cancelled()
    payment_services.mark_submitting(refund.pk, AT + timedelta(minutes=31))
    with override_settings(**PROD_FAKE_CLICK):
        tasks.submit_refund({"refund_id": refund.pk})
    refund.refresh_from_db()
    assert refund.state == "unknown" and refund.state in ATTENTION_REFUND_STATES
    assert fake_click.calls == []
    assert OrderEvent.objects.filter(order=order, type="refund_failed").exists()


def test_submit_of_a_payment_that_is_no_longer_paid() -> None:
    order, refund = _cancelled()
    Payment.objects.filter(order=order).update(status="refunded", refunded_at=AT + timedelta(hours=1))
    deliver("refund.submit", order.pk)
    refund.refresh_from_db()
    assert (refund.state, refund.error_code) == ("failed", tasks.PAYMENT_NOT_PAID)
    assert fake_click.calls == []
    assert OrderEvent.objects.filter(order=order, type="refund_failed").exists()


def test_fresh_status_check_reversed_closes_without_a_payout(staff_client: APIClient) -> None:
    order, refund = _failed()
    _set_status(order, "reversed")
    assert staff_client.post(_resolve_url(refund), {"state": "succeeded"}, format="json").status_code == 200
    refund.refresh_from_db()
    assert refund.state == "succeeded" and Payment.objects.get(order=order).status == "refunded"


def test_fresh_status_check_paid_needs_the_flag(staff_client: APIClient) -> None:
    order, refund = _failed()
    refused = staff_client.post(_resolve_url(refund), {"state": "succeeded"}, format="json")
    assert refused.status_code == 409 and refused.json()["message"] == services.MESSAGE_REFUND_STILL_PAID
    assert refused.json()["code"] == "click_not_reversed"  # M3: its own code (the UI offers the payout)
    refund.refresh_from_db()
    assert refund.state == "failed"
    ok = staff_client.post(_resolve_url(refund), {"state": "succeeded", "manual_payout": True}, format="json")
    assert ok.status_code == 200
    audit = AuditEntry.objects.filter(action="refund.state_changed", entity_id=str(refund.pk)).last()
    assert audit is not None and isinstance(audit.after, dict) and audit.after["manual_payout"] is True


def test_status_call_down_answers_503_and_writes_nothing(staff_client: APIClient) -> None:
    order, refund = _failed()
    _set_status(order, "down")
    response = staff_client.post(
        _resolve_url(refund), {"state": "succeeded", "manual_payout": True}, format="json"
    )
    assert response.status_code == 503 and response.json()["code"] == "provider_unavailable"
    refund.refresh_from_db()
    assert refund.state == "failed"


def test_fake_click_in_production_refuses_resolve_and_retry(staff_client: APIClient) -> None:
    order, refund = _failed()
    calls = len(fake_click.calls)
    with override_settings(**PROD_FAKE_CLICK):
        resolve = staff_client.post(
            _resolve_url(refund), {"state": "succeeded", "manual_payout": True}, format="json"
        )
        retry = staff_client.post(
            f"/api/v1/admin/payments/{refund.payment_id}/refunds/{refund.pk}/retry/", format="json"
        )
    assert resolve.status_code == 409 and resolve.json()["message"] == services.MESSAGE_REFUND_CLICK_FAKE
    assert retry.status_code == 409 and retry.json()["message"] == services.MESSAGE_REFUND_CLICK_FAKE
    assert Refund.objects.filter(payment__order=order).count() == 1 and len(fake_click.calls) == calls


def test_the_status_call_runs_before_any_row_lock() -> None:
    order, refund = _failed()
    sequence: list[str] = []
    real_lock = services.lock_order
    real_status = FakeClick.payment_status

    def lock(*args: Any, **kwargs: Any) -> Any:
        sequence.append("lock")
        return real_lock(*args, **kwargs)

    def status(self: FakeClick, ref: str) -> Any:
        sequence.append("status")
        return real_status(self, ref)

    with (
        mock.patch.object(services, "lock_order", lock),
        mock.patch.object(FakeClick, "payment_status", status),
    ):
        services.resolve_refund(refund.pk, "succeeded", admin().pk, manual_payout=True)
    assert sequence[0] == "status" and "lock" in sequence


def test_without_a_paydoc_no_check_and_the_flag_is_required() -> None:
    order, refund = _failed()
    payment = Payment.objects.get(order=order)
    payment.provider_state = {}
    payment.save(update_fields=["provider_state"])
    calls = len(fake_click.calls)
    with pytest.raises(Conflict) as refused:
        services.resolve_refund(refund.pk, "succeeded", admin().pk)
    # M3: Click was never asked, so the answer never says "Click has not reversed the payment"
    assert refused.value.detail == services.MESSAGE_REFUND_NO_PAYDOC
    assert services.MESSAGE_REFUND_NO_PAYDOC != services.MESSAGE_REFUND_STILL_PAID
    services.resolve_refund(refund.pk, "succeeded", admin().pk, manual_payout=True)
    assert len(fake_click.calls) == calls


def test_without_a_paydoc_the_api_answers_409_conflict_with_its_own_message(staff_client: APIClient) -> None:
    order, refund = _failed()
    Payment.objects.filter(order=order).update(provider_state={})
    calls = len(fake_click.calls)
    response = staff_client.post(_resolve_url(refund), {"state": "succeeded"}, format="json")
    assert response.status_code == 409
    assert (response.json()["code"], response.json()["message"]) == (
        "conflict",
        services.MESSAGE_REFUND_NO_PAYDOC,
    )
    assert len(fake_click.calls) == calls
    refund.refresh_from_db()
    assert refund.state == "failed"


def test_the_guard_runs_before_the_click_status_check(staff_client: APIClient) -> None:
    """M3: a failed refund that can no longer be closed (a retry superseded it) is refused without asking
    Click — not after a status call whose answer would be irrelevant (or a 503 when Click is down)."""
    order, refund = _failed()
    retry_url = f"/api/v1/admin/payments/{refund.payment_id}/refunds/{refund.pk}/retry/"
    assert staff_client.post(retry_url, format="json").status_code == 200
    _set_status(order, "down")  # a status call would answer 503
    calls = len(fake_click.calls)
    response = staff_client.post(_resolve_url(refund), {"state": "succeeded"}, format="json")
    assert response.status_code == 409 and response.json()["code"] == "invalid_transition"
    assert response.json()["message"] == services.MESSAGE_REFUND_NOT_RESOLVABLE
    assert len(fake_click.calls) == calls  # Click never asked


def test_the_resolve_guard_after_a_retry(staff_client: APIClient) -> None:
    order, refund = _failed()
    retry_url = f"/api/v1/admin/payments/{refund.payment_id}/refunds/{refund.pk}/retry/"
    assert staff_client.post(retry_url, format="json").status_code == 200
    newer = Refund.objects.filter(payment__order=order).order_by("-id").first()
    assert newer is not None and newer.state == "requested"
    response = staff_client.post(
        _resolve_url(refund), {"state": "succeeded", "manual_payout": True}, format="json"
    )
    assert response.status_code == 409 and response.json()["code"] == "invalid_transition"
    assert response.json()["message"] == services.MESSAGE_REFUND_NOT_RESOLVABLE
    assert Payment.objects.get(order=order).status == "paid"
    order.metadata = {}
    order.save(update_fields=["metadata"])
    deliver("refund.submit", order.pk)
    newer.refresh_from_db()
    assert newer.state == "succeeded"
    again = staff_client.post(
        _resolve_url(refund), {"state": "succeeded", "manual_payout": True}, format="json"
    )
    assert again.status_code == 409
    assert Refund.objects.filter(payment__order=order, state="succeeded").count() == 1


# --- attention.refunds_needing_action and payments/?refund_state=failed (admin §2.10, §2.12)
# -------------------


def test_a_superseded_failure_needs_no_action(staff_client: APIClient) -> None:
    open_failure = RefundFactory.create(failed=True)
    superseded_payment = PaymentFactory.create()
    superseded = RefundFactory.create(payment=superseded_payment, failed=True)
    RefundFactory.create(payment=superseded_payment, succeeded=True)
    refunded_payment = PaymentFactory.create(refunded=True)
    on_refunded = RefundFactory.create(payment=refunded_payment, failed=True)
    RefundFactory.create(manual_required=True)
    assert payment_selectors.refunds_needing_action() == 2  # open_failure + manual_required
    ids = [
        row["id"]
        for row in staff_client.get("/api/v1/admin/payments/", {"refund_state": "failed"}).json()["results"]
    ]
    assert ids == [open_failure.payment_id]
    assert superseded.pk and on_refunded.pk
    dashboard = staff_client.get("/api/v1/admin/dashboard/").json()
    assert dashboard["attention"]["refunds_needing_action"] == 2


def test_a_failure_after_an_older_success_still_counts() -> None:
    payment = PaymentFactory.create()
    RefundFactory.create(payment=payment, succeeded=True, amount=1)  # partial and older (defensive)
    RefundFactory.create(payment=payment, failed=True)
    assert payment_selectors.refunds_needing_action() == 1


# --- Refund.manual_reason (M3, admin §2.2 / §2.12) and the §5.1 line of the payments export -----------------


CLICK_MANUAL_LINE = "Mijozga qo‘lda to‘lang, so‘ng «Qaytarildi» deb belgilang"


def _api_refund(staff_client: APIClient, order: Order) -> dict[str, Any]:
    detail = staff_client.get(f"/api/v1/admin/orders/{order.pk}/").json()
    (payments_row,) = [
        row
        for row in staff_client.get("/api/v1/admin/payments/", {"search": str(order.pk)}).json()["results"]
        if row["order_id"] == order.pk
    ]
    assert detail["refunds"] == payments_row["refunds"]  # one Refund object on both screens
    return dict(detail["refunds"][0])


def _export_line(staff_client: APIClient, order: Order) -> str:
    response = staff_client.get("/api/v1/admin/payments/", {"export": "csv", "search": str(order.pk)})
    lines = response.content.decode("utf-8-sig").split("\r\n")
    (line,) = [line for line in lines[1:] if line.startswith(f'"{Payment.objects.get(order=order).pk}";')]
    return str(line.rsplit(";", 1)[1].strip('"'))


@pytest.mark.parametrize(
    ("provider", "paydoc", "settings_", "reason", "line"),
    [
        ("payme", True, {}, "payme", "Payme kabinetida qaytarish kerak"),
        ("click", False, {}, "no_paydoc", "Mijozga qo‘lda to‘lang, so‘ng «Qaytarildi» deb belgilang"),
        (
            "click",
            True,
            PROD_FAKE_CLICK,
            "fake_mode",
            "Mijozga qo‘lda to‘lang, so‘ng «Qaytarildi» deb belgilang",
        ),
    ],
)
def test_a_manual_refund_carries_its_reason_and_the_export_line_by_provider(
    staff_client: APIClient,
    provider: str,
    paydoc: bool,
    settings_: dict[str, Any],
    reason: str,
    line: str,
) -> None:
    order = place(fulfilment="pickup", provider=provider)
    pay(order, paydoc=paydoc)
    with override_settings(**settings_), pinned_clock(AT + timedelta(minutes=30)):
        services.cancel_order(order.pk, "Mijoz so‘rovi bilan", admin().pk)
    refund = Refund.objects.get(payment__order=order)
    assert (refund.state, refund.manual_reason) == ("manual_required", reason)
    assert _api_refund(staff_client, order)["manual_reason"] == reason
    assert _export_line(staff_client, order) == line
    event = OrderEvent.objects.get(order=order, type="refund_requested")
    assert event.data["manual_reason"] == reason
    # the reason stays once the refund is closed (it says why it was manual)
    services.resolve_refund(refund.pk, "succeeded", admin().pk)
    assert _api_refund(staff_client, order)["manual_reason"] == reason
    assert _export_line(staff_client, order) == "Qaytarildi"


def test_a_click_refund_sent_to_click_has_no_manual_reason(staff_client: APIClient) -> None:
    order, refund = _cancelled()
    assert refund.manual_reason == "" and _api_refund(staff_client, order)["manual_reason"] is None
    assert _export_line(staff_client, order) == "Qaytarish so‘raldi"


def test_the_submit_recheck_stores_the_reason_it_found(staff_client: APIClient) -> None:
    """A Click refund that turns manual_required in the refund.submit re-check (Click switched to fake) has no
    event saying why: the reason is stored on the refund, so the API and the export never fall back to
    Payme."""
    order, refund = _cancelled()
    with override_settings(**PROD_FAKE_CLICK):
        deliver("refund.submit", order.pk)
    refund.refresh_from_db()
    assert (refund.state, refund.manual_reason) == ("manual_required", "fake_mode")
    assert _api_refund(staff_client, order)["manual_reason"] == "fake_mode"
    assert _export_line(staff_client, order) == CLICK_MANUAL_LINE
    audit = AuditEntry.objects.filter(action="refund.state_changed", entity_id=str(refund.pk)).last()
    assert audit is not None and isinstance(audit.after, dict) and audit.after["manual_reason"] == "fake_mode"


def test_a_manual_click_refund_stored_without_a_reason_never_reads_as_payme(staff_client: APIClient) -> None:
    """Rows stored without a reason (factory rows): the fallback derives the Click reason from the
    payment."""
    order = place(fulfilment="pickup")
    pay(order, paydoc=False)
    payment = Payment.objects.get(order=order)
    RefundFactory.create(payment=payment, state="manual_required", manual_reason="")
    assert _api_refund(staff_client, order)["manual_reason"] == "no_paydoc"
    assert _export_line(staff_client, order) == CLICK_MANUAL_LINE


def test_the_backfill_places_the_reason_of_existing_refunds() -> None:
    """commerce.0008: the event's reason, else Payme → payme, Click → no_paydoc / fake_mode by the payment's
    Click id; refunds that were never manual stay blank."""
    from importlib import import_module

    from django.db import connection

    backfill = import_module("glowderma.commerce.migrations.0008_backfill_refund_manual_reason").BACKFILL
    payme_order, payme_refund = _cancelled("payme")
    no_paydoc_order, no_paydoc_refund = _cancelled(paydoc=False)
    sent_order, sent_refund = _cancelled()
    fake_order = place(fulfilment="pickup")
    pay(fake_order)
    with override_settings(**PROD_FAKE_CLICK), pinned_clock(AT + timedelta(minutes=30)):
        services.cancel_order(fake_order.pk, "Mijoz so‘rovi bilan", admin().pk)
    fake_refund = Refund.objects.get(payment__order=fake_order)
    # a manual Click refund with a Click id and no event (the submit re-check): fake_mode by elimination
    recheck = RefundFactory.create(state="manual_required", manual_reason="")
    Payment.objects.filter(pk=recheck.payment_id).update(provider_state={"click_paydoc_id": "5500000001"})
    Refund.objects.update(manual_reason="")
    with connection.cursor() as cursor:
        cursor.execute(backfill)
    reasons = dict(Refund.objects.values_list("pk", "manual_reason"))
    assert reasons[payme_refund.pk] == "payme"
    assert reasons[no_paydoc_refund.pk] == "no_paydoc"
    assert reasons[fake_refund.pk] == "fake_mode"  # from its event (its payment has a Click id)
    assert reasons[recheck.pk] == "fake_mode"
    assert reasons[sent_refund.pk] == ""
    assert payme_order.pk and no_paydoc_order.pk and sent_order.pk
