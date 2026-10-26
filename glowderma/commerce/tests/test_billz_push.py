"""``billz.push_sale`` over the Billz port (M3): the live push end to end through the outbox dispatcher
(recorded Billz answers, no network), resume after a crash, permanent refusals dead-lettered at once, the
fake-in-production guarantee (customer contract §6.14) and orders cancelled before the push."""

import logging
from datetime import timedelta
from io import StringIO
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from django.core.cache import cache
from django.core.management import call_command
from django.test import override_settings
from redis.exceptions import ConnectionError as RedisConnectionError

from glowderma.catalog.models import Product
from glowderma.commerce import services, tasks, topics
from glowderma.commerce.models import OrderEvent
from glowderma.commerce.tests.support import AT, admin, pay, place, product, shopper
from glowderma.core import timeutils
from glowderma.core.api.exceptions import Conflict
from glowderma.core.models import AuditEntry, OutboxMessage
from glowderma.core.seeding import pinned_clock
from glowderma.core.tasks import dispatch_outbox
from glowderma.integrations.billz.client import BillzClient
from glowderma.integrations.billz.live import SALE_BUSY_RETRY, SALE_LOCK_KEY, LiveBillz
from glowderma.integrations.billz.ports import BillzPaymentPending
from glowderma.integrations.tests.billz_support import (
    CLIENT_ID,
    FOAM_ID,
    SALE_ID,
    SERUM_ID,
    BillzServer,
    config,
    fixture,
    live,
)
from glowderma.loyalty import selectors as loyalty_selectors
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
    assert "billz.sale_dead order_id=None purchase_id=None billz_order_id=- reason=boom" in caplog.text


# --- review fixes -------------------------------------------------------------------------------------------


@override_settings(BILLZ_MODE="live")
def test_the_live_sale_is_paid_at_what_the_customer_was_charged() -> None:
    """Decision D1: free price per line, payment = Σ unit_price × quantity (cashback and delivery outside)."""
    order = paid_order()
    server = BillzServer()
    server.prices = {SERUM_ID: 199_000, FOAM_ID: 99_000}  # Billz's own prices differ from the admin's
    with patch("glowderma.commerce.billz_sales.get_billz", return_value=live(server)):
        message = dispatch(message_of(order.pk))
    assert message.status == "done", message.last_error
    items = {
        body["product_id"]: body["free_price"] for body in server.bodies("POST /v2/order-product/{order_id}")
    }
    assert items == {SERUM_ID: 185_000, FOAM_ID: 95_000}
    (payment,) = server.sales[SALE_ID].payments
    assert payment["payments"][0]["paid_amount"] == 2 * 185_000 + 95_000 == order.items_subtotal


@override_settings(BILLZ_MODE="live")
def test_a_bill_that_is_not_what_the_customer_paid_is_dead_for_staff(
    caplog: pytest.LogCaptureFixture,
) -> None:
    order = paid_order()
    server = BillzServer()

    def discounted(request: httpx.Request) -> httpx.Response:
        bill = fixture("recalculate")
        bill["total_price"] = 400_000  # e.g. a Billz client discount on top of the free prices
        return httpx.Response(200, json=bill)

    server.overrides["POST /v1/recalculate-order-bill/{order_id}"] = discounted
    with (
        patch("glowderma.commerce.billz_sales.get_billz", return_value=live(server)),
        caplog.at_level(logging.ERROR),
    ):
        message = dispatch(message_of(order.pk))
    assert (message.status, message.attempts) == ("dead", 1)
    assert "Billz bills 400000 so'm" in message.last_error and "charged 465000" in message.last_error
    assert server.count("POST /v2/order-payment/{order_id}") == 0
    assert (
        f"billz.sale_dead order_id={order.pk}" in caplog.text and f"billz_order_id={SALE_ID}" in caplog.text
    )


def second_order() -> Any:
    """Another paid order of the products ``paid_order`` created."""
    serum = Product.objects.get(billz_id="4607012301")
    return pay(place(shopper(phone="+998901234568"), [(serum, 1)], fulfilment="pickup"))


@override_settings(BILLZ_MODE="live", BILLZ_SHIFT_RETRY_MINUTES=15, BILLZ_SHIFT_RETRY_MAX_HOURS=36)
def test_a_closed_shift_is_deferred_without_spending_attempts_then_dead_after_the_window() -> None:
    """Decision D2 (reviews A M3, B B8): 20082 is not permanent — retried every 15 minutes without spending
    the outbox attempts, for at most 36 hours after the first refusal, then dead "shift closed"."""
    order = paid_order()
    server = BillzServer()
    server.overrides["POST /v2/order-payment/{order_id}"] = lambda request: httpx.Response(
        400, json={**fixture("payment_error"), "data": {"error_code": 20082, "message": "cashbox closed"}}
    )
    start = timeutils.now()
    with patch("glowderma.commerce.billz_sales.get_billz", side_effect=lambda **kwargs: live(server)):
        with pinned_clock(start):
            message = dispatch(message_of(order.pk))
        assert (message.status, message.attempts) == ("failed", 0)
        assert message.available_at == start + timedelta(minutes=15)
        assert message.last_error.startswith(f"shift closed: Billz refused the sale of order {order.pk}")
        purchase = Purchase.objects.get(order=order)
        assert purchase.billz_payment_attempted_at is None  # refused: certainly not paid
        # the next shift opens: the draft is resumed and paid once
        server.overrides.pop("POST /v2/order-payment/{order_id}")
        with pinned_clock(start + timedelta(minutes=15)):
            message = dispatch(message)
    assert message.status == "done" and server.count("POST /v2/order") == 1
    assert len(server.sales[SALE_ID].payments) == 1
    # a sale whose shift never opens again within the window
    other = second_order()
    server.overrides["POST /v2/order-payment/{order_id}"] = lambda request: httpx.Response(
        400, json={**fixture("payment_error"), "data": {"error_code": 20010, "message": "no cashbox"}}
    )
    with patch("glowderma.commerce.billz_sales.get_billz", side_effect=lambda **kwargs: live(server)):
        with pinned_clock(start):
            first = dispatch(message_of(other.pk))
        assert (first.status, first.deferred_since) == ("failed", start)
        with pinned_clock(start + timedelta(hours=36)):
            dead = dispatch(first)
    assert dead.status == "dead" and dead.last_error.startswith("shift closed: ")
    assert "gave up after 36 h" in dead.last_error
    assert AuditEntry.objects.filter(action="outbox.dead", entity_id=str(dead.pk)).exists()


@override_settings(BILLZ_MODE="live")
def test_a_lost_payment_answer_is_deferred_never_paid_twice() -> None:
    """Review B2 end to end: the attempt is committed before the request; the next delivery waits for the
    quiet period without spending attempts; Billz then shows the sale paid and the push completes."""
    order = paid_order()
    server = BillzServer()
    original = server._default
    lost = [True]

    def payment(request: httpx.Request) -> httpx.Response:
        response = original("POST /v2/order-payment/{order_id}", request)  # Billz takes the payment …
        if lost.pop() if lost else False:
            raise httpx.ReadTimeout("timed out", request=request)  # … and the answer is lost
        return response

    server.overrides["POST /v2/order-payment/{order_id}"] = payment
    real_sale_detail = server._detail

    def lagging(sale: Any) -> dict[str, Any]:
        body = real_sale_detail(sale)
        body["finished_at"] = "0001-01-01T00:00:00Z"  # Billz shows the close only later
        return body

    server._detail = lagging  # type: ignore[method-assign]
    start = timeutils.now()
    with patch("glowderma.commerce.billz_sales.get_billz", side_effect=lambda **kwargs: live(server)):
        with pinned_clock(start):
            first = dispatch(message_of(order.pk))
        assert (first.status, first.attempts) == ("failed", 1) and "ReadTimeout" in first.last_error
        assert Purchase.objects.get(order=order).billz_payment_attempted_at == start
        with pinned_clock(start + timedelta(seconds=2)):
            second = dispatch(first)
        assert (second.status, second.attempts) == ("failed", 1)  # deferred: no attempt spent
        assert second.available_at == start + timedelta(minutes=10)
        assert "pending" in second.last_error
        server._detail = real_sale_detail  # type: ignore[method-assign]
        with pinned_clock(start + timedelta(minutes=10)):
            third = dispatch(second)
    assert third.status == "done"
    assert server.count("POST /v2/order-payment/{order_id}") == 1 and len(server.sales[SALE_ID].payments) == 1
    assert OrderEvent.objects.filter(order=order, type="billz_sale_recorded").count() == 1


class OneShotBlip:
    """The Django cache, failing the next call of each operation named in ``ops`` like a Redis blip."""

    def __init__(self) -> None:
        self.ops: set[str] = set()

    def __getattr__(self, name: str) -> Any:
        real = getattr(cache, name)

        def call(*args: Any, **kwargs: Any) -> Any:
            if name in self.ops:
                self.ops.discard(name)
                raise RedisConnectionError("Redis blip")
            return real(*args, **kwargs)

        return call


@override_settings(BILLZ_MODE="live")
def test_a_cache_blip_right_after_the_payment_never_pays_twice() -> None:
    """NEW-1 end to end (probe test_defect_e2e_cache_blip_after_payment_pays_twice): Billz takes the payment
    and Redis blips during the circuit bookkeeping right after the answer. The push completes on that answer;
    the attempt is never forgotten and Billz is paid once."""
    order = paid_order()
    server = BillzServer()
    blip = OneShotBlip()
    original = server._default

    def payment(request: httpx.Request) -> httpx.Response:
        response = original("POST /v2/order-payment/{order_id}", request)
        blip.ops = {"delete"}
        return response

    server.overrides["POST /v2/order-payment/{order_id}"] = payment
    cfg = config()

    def adapter(**_: Any) -> LiveBillz:
        flaky: Any = blip  # duck-typed like the Django cache
        return LiveBillz(cfg, cache=flaky, client=BillzClient(cfg, transport=server.transport, cache=flaky))

    with patch("glowderma.commerce.billz_sales.get_billz", side_effect=adapter):
        first = dispatch(message_of(order.pk))
        assert first.status == "done", first.last_error
        dispatch(first)
    assert len(server.sales[SALE_ID].payments) == 1
    purchase = Purchase.objects.get(order=order)
    assert purchase.billz_pushed_at is not None and purchase.billz_payment_attempted_at is not None


@override_settings(BILLZ_MODE="live")
def test_a_sale_another_worker_is_pushing_is_deferred_without_spending_an_attempt() -> None:
    """NEW-9: the sale lock held by another worker is a deferral (``RetryLater``), not a failure."""
    order = paid_order()
    purchase = Purchase.objects.get(order=order)
    key = SALE_LOCK_KEY.format(order_uuid=purchase.billz_order_uuid)
    cache.add(key, "another-worker", timeout=60)
    server = BillzServer()
    start = timeutils.now()
    with patch("glowderma.commerce.billz_sales.get_billz", return_value=live(server)), pinned_clock(start):
        message = dispatch(message_of(order.pk))
    assert (message.status, message.attempts) == ("failed", 0)
    assert message.available_at == start + SALE_BUSY_RETRY and "busy" in message.last_error
    assert server.requests == []
    cache.delete(key)
    with patch("glowderma.commerce.billz_sales.get_billz", return_value=live(server)):
        assert dispatch(message).status == "done"


@override_settings(BILLZ_MODE="live")
def test_an_order_cancelled_during_the_push_is_never_paid(
    caplog: pytest.LogCaptureFixture, django_capture_on_commit_callbacks: Any
) -> None:
    """Reviews A S5 / B B11 (probe test_order_cancelled_during_the_push_is_still_paid_and_recorded): the
    order is re-checked under its lock right before the payment; the draft is named for staff."""
    order = paid_order()
    server = BillzServer()
    staff = admin()
    original = server._default

    def recalc(request: httpx.Request) -> httpx.Response:
        services.cancel_order(order.pk, "Mijoz so‘rovi bilan", staff.pk)  # an admin cancels meanwhile
        return original("POST /v1/recalculate-order-bill/{order_id}", request)

    server.overrides["POST /v1/recalculate-order-bill/{order_id}"] = recalc
    with (
        patch("glowderma.commerce.billz_sales.get_billz", return_value=live(server)),
        caplog.at_level(logging.WARNING),
        django_capture_on_commit_callbacks(execute=True),
    ):
        message = dispatch(message_of(order.pk))
    order.refresh_from_db()
    assert order.status == "cancelled" and message.status == "done"
    assert server.count("POST /v2/order-payment/{order_id}") == 0
    purchase = Purchase.objects.get(order=order)
    assert (purchase.billz_pushed_at, purchase.billz_sale_id) == (None, SALE_ID)
    assert not OrderEvent.objects.filter(order=order, type="billz_sale_recorded").exists()
    assert f"billz.sale_of_cancelled_order order_id={order.pk} billz_order_id={SALE_ID}" in caplog.text
    assert f"billz.refund_by_hand order_id={order.pk} billz_order_id={SALE_ID} pushed=False" in caplog.text


@override_settings(BILLZ_MODE="live")
def test_an_order_cancelled_while_its_payment_was_in_flight_is_linked_without_the_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    order = paid_order()
    server = BillzServer()
    staff = admin()
    original = server._default

    def payment(request: httpx.Request) -> httpx.Response:
        services.cancel_order(order.pk, "Mijoz so‘rovi bilan", staff.pk)  # after before_payment committed
        return original("POST /v2/order-payment/{order_id}", request)

    server.overrides["POST /v2/order-payment/{order_id}"] = payment
    with (
        patch("glowderma.commerce.billz_sales.get_billz", return_value=live(server)),
        caplog.at_level(logging.ERROR),
    ):
        message = dispatch(message_of(order.pk))
    assert message.status == "done"
    purchase = Purchase.objects.get(order=order)
    assert purchase.billz_pushed_at is not None  # the facts: Billz holds this paid sale
    assert not OrderEvent.objects.filter(order=order, type="billz_sale_recorded").exists()
    assert "the order was cancelled while its sale was paid in Billz" in caplog.text


@override_settings(BILLZ_MODE="live")
def test_a_stale_read_before_the_lock_never_creates_a_second_sale() -> None:
    """Review B nit: the purchase is re-read under the sale lock; an attempt that read it before another
    attempt finished the push returns the recorded sale instead of creating a second one."""
    order = paid_order()
    server = BillzServer()
    payload = message_of(order.pk).payload
    with patch("glowderma.commerce.billz_sales.get_billz", side_effect=lambda **kwargs: live(server)):
        real_info = loyalty_selectors.billz_push_info
        stale = real_info(int(payload["purchase_id"]))  # read before the other attempt ran
        tasks.push_sale(payload)  # the other attempt finishes the push
        reads = iter([stale])

        def first_read_stale(purchase_id: int) -> Any:
            return next(reads, None) or real_info(purchase_id)

        with patch.object(loyalty_selectors, "billz_push_info", side_effect=first_read_stale):
            tasks.push_sale(payload)
    assert server.count("POST /v2/order") == 1 and server.count("POST /v2/order-payment/{order_id}") == 1


@override_settings(BILLZ_MODE="live")
def test_a_sale_dead_on_a_configuration_error_is_retried_then_replayed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Decision D3 (review B8): a 403 (the key lost the sale routes) is retryable with an ERROR that alerts;
    once the key is fixed ``manage.py requeue_outbox`` replays what died meanwhile."""
    order = paid_order()
    server = BillzServer()
    server.overrides["POST /v2/order"] = lambda request: httpx.Response(403, json=fixture("forbidden"))
    with patch("glowderma.commerce.billz_sales.get_billz", side_effect=lambda **kwargs: live(server)):
        with caplog.at_level(logging.ERROR):
            first = dispatch(message_of(order.pk))
        assert (first.status, first.attempts) == ("failed", 1)
        assert "billz.configuration_error" in caplog.text
        OutboxMessage.objects.filter(pk=first.pk).update(attempts=9)
        dead = dispatch(first)
        assert dead.status == "dead"
        server.overrides.pop("POST /v2/order")
        out = StringIO()
        call_command("requeue_outbox", "--topic", topics.BILLZ_PUSH_SALE, "--dead", stdout=out)
        assert "1 dead message(s) matched, 1 requeued" in out.getvalue()
        replayed = dispatch(dead)
    assert replayed.status == "done" and Purchase.objects.get(order=order).billz_pushed_at is not None


def test_cancelling_an_order_whose_billz_sale_exists_asks_for_a_refund_by_hand(
    caplog: pytest.LogCaptureFixture, django_capture_on_commit_callbacks: Any
) -> None:
    order = paid_order()
    Purchase.objects.filter(order=order).update(
        billz_sale_id=SALE_ID, billz_sale_ref="5632631379", billz_pushed_at=AT
    )
    with caplog.at_level(logging.ERROR), django_capture_on_commit_callbacks(execute=True):
        services.cancel_order(order.pk, "Mijoz so‘rovi bilan", admin().pk)
    assert f"billz.refund_by_hand order_id={order.pk} billz_order_id={SALE_ID} pushed=True" in caplog.text


def test_the_payment_recheck_waits_for_the_order_row_only_briefly() -> None:
    """An admin cancel holding the Order row (a BTS call) makes the push fail and retry before paying,
    instead of holding the push past its deadline."""
    from glowderma.commerce import billz_sales

    order = paid_order()
    purchase = Purchase.objects.get(order=order)
    assert purchase.billz_order_uuid is not None
    journal = billz_sales.PurchaseJournal(purchase.pk, purchase.billz_order_uuid, order.pk)
    with patch.object(services, "lock_order", side_effect=Conflict("busy")) as lock:
        with pytest.raises(Conflict):
            journal.before_payment()
    assert lock.call_args.kwargs == {"wait": billz_sales.ORDER_LOCK_WAIT}
    assert Purchase.objects.get(pk=purchase.pk).billz_payment_attempted_at is None


def test_the_payment_attempt_is_a_compare_and_set_under_the_order_lock() -> None:
    """NEW-5: an attempt that read the purchase before another attempt stamped (and maybe sent) the payment
    never stamps it again: it is deferred (no attempt spent) and the next one reads the sale first."""
    from glowderma.commerce import billz_sales
    from glowderma.loyalty import services as loyalty_services

    order = paid_order()
    purchase = Purchase.objects.get(order=order)
    assert purchase.billz_order_uuid is not None
    loyalty_services.record_billz_order(
        purchase.pk, billz_order_uuid=purchase.billz_order_uuid, billz_sale_id=SALE_ID, billz_sale_ref="1"
    )
    journal = billz_sales.PurchaseJournal(purchase.pk, purchase.billz_order_uuid, order.pk)
    journal.before_payment()  # the first attempt stamps it
    stamped = Purchase.objects.get(pk=purchase.pk).billz_payment_attempted_at
    assert stamped is not None
    with pinned_clock(timeutils.now() + timedelta(seconds=30)), pytest.raises(BillzPaymentPending) as caught:
        journal.before_payment()  # a second attempt that read the purchase before the stamp
    assert caught.value.retry_after == billz_sales.PAYMENT_RACE_RETRY
    assert Purchase.objects.get(pk=purchase.pk).billz_payment_attempted_at == stamped  # unchanged
