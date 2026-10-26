"""``billz.push_sale`` over the Billz port (M3): the live push end to end through the outbox dispatcher
(recorded Billz answers, no network), resume after a crash, permanent refusals dead-lettered at once, the
fake-in-production guarantee (customer contract §6.14) and orders cancelled before the push."""

import logging
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest
from django.test import override_settings

from glowderma.commerce import services, tasks, topics
from glowderma.commerce.models import OrderEvent
from glowderma.commerce.tests.support import AT, admin, pay, place, product, shopper
from glowderma.core.models import AuditEntry, OutboxMessage
from glowderma.core.tasks import dispatch_outbox
from glowderma.integrations.tests.billz_support import (
    CLIENT_ID,
    FOAM_ID,
    SALE_ID,
    SERUM_ID,
    BillzServer,
    live,
)
from glowderma.loyalty.models import Purchase

pytestmark = pytest.mark.django_db


def paid_order(**kwargs: Any) -> Any:
    serum = product(185_000, billz_id="4607012301")
    foam = product(95_000, billz_id="4607012302")
    customer = shopper(phone="+998901234567")
    return pay(place(customer, [(serum, 2), (foam, 1)], fulfilment="pickup", **kwargs))


def message_of(order_id: int) -> OutboxMessage:
    return OutboxMessage.objects.get(topic=topics.BILLZ_PUSH_SALE, payload__order_id=order_id)


def dispatch(message: OutboxMessage) -> OutboxMessage:
    """One dispatcher round for this message only (others are parked as done)."""
    OutboxMessage.objects.filter(pk=message.pk).update(available_at=AT - timedelta(days=1))
    OutboxMessage.objects.exclude(pk=message.pk).update(status="done")
    dispatch_outbox()
    message.refresh_from_db()
    return message


@override_settings(BILLZ_MODE="live")
def test_live_push_records_the_billz_sale_on_the_purchase() -> None:
    order = paid_order()
    server = BillzServer()
    with patch("glowderma.commerce.billz_sales.get_billz", return_value=live(server)):
        message = dispatch(message_of(order.pk))
    assert message.status == "done", message.last_error
    purchase = Purchase.objects.get(order=order)
    assert (purchase.billz_sale_id, purchase.billz_sale_ref) == (SALE_ID, "5632631379")
    assert purchase.billz_pushed_at is not None
    event = OrderEvent.objects.get(order=order, type="billz_sale_recorded")
    assert event.data == {"billz_order_id": SALE_ID}  # Billz's own sale id, findable in Billz
    sale = server.sales[SALE_ID]
    assert sale.items == {SERUM_ID: 2.0, FOAM_ID: 1.0} and sale.customer_id == CLIENT_ID
    (payment,) = sale.payments
    assert payment["comment"] == f"Glow Derma #{order.pk}" and payment["without_cashback"] is True
    (lookup,) = [r for r in server.requests if server.label(r) == "GET /v1/client"]
    assert lookup.url.params["phone_number"] == "+998901234567"  # the customer's loyalty phone


@override_settings(BILLZ_MODE="live")
@pytest.mark.parametrize(
    "crash_after", ["POST /v2/order-product/{order_id}", "POST /v2/order-payment/{order_id}"]
)
def test_a_crash_after_billz_applied_a_step_resumes_on_the_persisted_sale(crash_after: str) -> None:
    order = paid_order()
    server = BillzServer()
    server.crash_after.add(crash_after)
    with patch("glowderma.commerce.billz_sales.get_billz", side_effect=lambda **kwargs: live(server)):
        first = dispatch(message_of(order.pk))
        assert first.status == "failed" and "POST" in first.last_error
        purchase = Purchase.objects.get(order=order)
        # persisted the moment Billz created the sale, before anything else; the push is not done yet
        assert (purchase.billz_sale_id, purchase.billz_pushed_at) == (SALE_ID, None)
        second = dispatch(first)
    assert second.status == "done"
    assert list(server.sales) == [SALE_ID] and server.count("POST /v2/order") == 1
    assert len(server.sales[SALE_ID].payments) == 1
    assert server.count("POST /v2/order-payment/{order_id}") == 1
    assert Purchase.objects.get(order=order).billz_pushed_at is not None
    assert OrderEvent.objects.filter(order=order, type="billz_sale_recorded").count() == 1


def test_a_permanent_refusal_is_dead_at_once_and_surfaces_to_staff(caplog: pytest.LogCaptureFixture) -> None:
    order = paid_order(fake={"billz": {"push": "rejected"}})
    with caplog.at_level(logging.ERROR):
        message = dispatch(message_of(order.pk))
    assert (message.status, message.attempts) == ("dead", 1)
    assert message.last_error.startswith(f"Billz refused the sale of order {order.pk}: product not found")
    entry = AuditEntry.objects.get(action="outbox.dead", entity_id=str(message.pk))
    after = entry.after or {}
    assert after["topic"] == "billz.push_sale" and "20036" in after["last_error"]
    assert "billz.sale_rejected" in caplog.text and "billz.sale_dead" in caplog.text
    assert Purchase.objects.get(order=order).billz_pushed_at is None
    assert not OrderEvent.objects.filter(order=order, type="billz_sale_recorded").exists()


def test_a_transient_failure_is_retried_not_dead() -> None:
    order = paid_order(fake={"billz": {"push": "down"}})
    message = dispatch(message_of(order.pk))
    assert (message.status, message.attempts) == ("failed", 1)
    assert "fake: down" in message.last_error


@override_settings(DEV_FEATURES_ALLOWED=False)
def test_a_fake_billz_under_the_production_settings_records_no_sale(caplog: pytest.LogCaptureFixture) -> None:
    """Customer contract §6.14: the handler does not call the fake, leaves billz_pushed_at null and warns,
    so staff enter the sale in Billz by hand and the live adapter can find every unpushed online sale."""
    with override_settings(DEV_FEATURES_ALLOWED=True):
        order = paid_order()
    with (
        patch("glowderma.commerce.billz_sales.get_billz", side_effect=AssertionError("the fake was called")),
        caplog.at_level(logging.WARNING),
    ):
        tasks.handle_billz_push_sale(message_of(order.pk))
    purchase = Purchase.objects.get(order=order)
    assert (purchase.billz_pushed_at, purchase.billz_sale_id) == (None, "")
    assert not OrderEvent.objects.filter(order=order, type="billz_sale_recorded").exists()
    assert f"billz.sale_not_pushed order_id={order.pk} purchase_id={purchase.pk}" in caplog.text


def test_an_order_cancelled_before_the_push_is_not_sold_in_billz(caplog: pytest.LogCaptureFixture) -> None:
    order = paid_order()
    services.cancel_order(order.pk, "Mijoz so‘rovi bilan", admin().pk)
    with (
        patch("glowderma.commerce.billz_sales.get_billz", side_effect=AssertionError("Billz was called")),
        caplog.at_level(logging.WARNING),
    ):
        tasks.handle_billz_push_sale(message_of(order.pk))
    assert Purchase.objects.get(order=order).billz_pushed_at is None
    assert "billz.sale_skipped_cancelled" in caplog.text
    # a Billz sale an earlier attempt created is named for staff (draft to delete or sale to refund)
    Purchase.objects.filter(order=order).update(billz_sale_id=SALE_ID, billz_sale_ref="5632631379")
    with caplog.at_level(logging.ERROR):
        tasks.handle_billz_push_sale(message_of(order.pk))
    assert f"billz.sale_of_cancelled_order order_id={order.pk} billz_order_id={SALE_ID}" in caplog.text


def test_dead_hook_tolerates_a_message_without_ids(caplog: pytest.LogCaptureFixture) -> None:
    message = OutboxMessage(topic=topics.BILLZ_PUSH_SALE, payload={}, dedupe_key="x", last_error="boom")
    with caplog.at_level(logging.ERROR):
        tasks.on_billz_push_dead(message)
    assert "billz.sale_dead order_id=None purchase_id=None reason=boom" in caplog.text
