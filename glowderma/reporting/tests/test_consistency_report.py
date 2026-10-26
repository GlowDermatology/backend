"""reporting.consistency_report: the nightly checks K1–K6 plus the loyalty reconciliation, the audit row, the
ERROR line and the alert (docs/ops/monitoring.md)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from io import StringIO
from typing import Any

import pytest
from celery.exceptions import SoftTimeLimitExceeded  # type: ignore[import-untyped]
from django.core.management import call_command
from django.db import connection, transaction
from django.test import override_settings

from glowderma.commerce.models import Order
from glowderma.commerce.tests.factories import OrderFactory
from glowderma.core import alerts, enums, timeutils
from glowderma.core.models import AuditEntry, OutboxMessage
from glowderma.delivery.tests.factories import ShipmentFactory
from glowderma.loyalty import tasks as loyalty_tasks
from glowderma.loyalty.tests.factories import PurchaseFactory, reverse_purchase_rows
from glowderma.payments import selectors as payment_selectors
from glowderma.payments.tests.factories import PaymentFactory, RefundFactory
from glowderma.reporting import tasks

pytestmark = pytest.mark.django_db

NOW = datetime.fromisoformat("2026-10-27T03:40:00+05:00")
CHECKS = [
    "k1_billz_sale_unrecorded",
    "k1_billz_sale_unrecorded_backlog",
    "k1_billz_sale_dead",
    "k2_payment_pending_stuck",
    "k2_click_paid_without_paydoc",
    "k3_refund_open",
    "k3_refund_failed",
    "k4_shipment_missing",
    "k4_parcel_untracked",
    "k5_late_capture_unrefunded",
    "k6_outbox_dead",
    "k6_outbox_overdue",
    "k6_outbox_deferred_past_window",
    "loyalty_reconciliation",
]


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(timeutils, "now", lambda: NOW)


def ago(**delta: float) -> datetime:
    return NOW - timedelta(**delta)


def run(**overrides: Any) -> dict[str, Any]:
    with override_settings(**overrides):
        return tasks.run_consistency_report()


def check(report: dict[str, Any], name: str) -> dict[str, Any]:
    return dict(report["checks"][name])


def test_a_clean_database_passes_every_check_and_is_audited(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="glowderma.reporting.tasks"):
        report = tasks.consistency_report.apply().get()  # the Celery task itself, in-process
    assert list(report["checks"]) == CHECKS
    assert report["mismatches"] == {}
    assert report["generated_at"] == "2026-10-27T03:40:00+05:00"
    assert check(report, "k1_billz_sale_unrecorded") == {"count": 0, "ids": [], "skipped": "billz_not_live"}
    assert check(report, "k6_outbox_dead") == {"count": 0, "ids": []}
    assert check(report, "loyalty_reconciliation") == {
        "count": 0,
        "ids": [],
        "checks": {
            "ledger_sum": 0,
            "last_balance_after": 0,
            "chain": 0,
            "purchase_links": 0,
            "captured_holds": 0,
        },
        "negative_balances": 0,
    }
    entry = AuditEntry.objects.get(action="report.consistency")
    assert (entry.actor_type, entry.entity_type, entry.entity_id) == ("system", "report", "consistency")
    assert entry.after == {"checks": report["checks"], "mismatches": {}}
    assert AuditEntry.objects.filter(action="loyalty.reconciliation_completed").count() == 1
    assert "report.consistency ok" in caplog.text
    assert "report.consistency_mismatch" not in caplog.text


def test_k1_unrecorded_and_dead_billz_sales() -> None:
    unrecorded = PurchaseFactory.create(order__created_at=ago(hours=3, minutes=5))
    no_sale_id = PurchaseFactory.create(
        order__created_at=ago(hours=5), billz_pushed_at=ago(hours=4), billz_sale_id=""
    )
    PurchaseFactory.create(order__created_at=ago(minutes=30))  # paid 27 min ago: still within 2 h
    PurchaseFactory.create(order__created_at=ago(hours=5), billz_pushed_at=ago(hours=4), billz_sale_id="b-1")
    backlog = PurchaseFactory.create(order__created_at=ago(days=8))  # before the 7-day look-back
    ancient = PurchaseFactory.create(order__created_at=ago(days=400), billz_pushed_at=ago(days=399))
    PurchaseFactory.create(
        order__created_at=ago(days=400), billz_pushed_at=ago(days=399), billz_sale_id="b-2"
    )
    reverse_purchase_rows(PurchaseFactory.create(order__created_at=ago(days=30)), at=ago(days=29))
    cancelled = PurchaseFactory.create(order__created_at=ago(hours=5))
    assert unrecorded.order_id and no_sale_id.order_id and cancelled.order_id
    assert backlog.order_id and ancient.order_id
    Order.objects.filter(pk=cancelled.order_id).update(
        status=enums.OrderStatus.CANCELLED,
        cancelled_at=ago(hours=4),
        cancellation_reason=enums.CancellationReason.CUSTOMER_REQUEST,
    )
    OutboxMessage.objects.create(
        topic="billz.push_sale",
        payload={"order_id": 10777, "purchase_id": 1},
        dedupe_key="b:1",
        status="dead",
    )
    OutboxMessage.objects.create(
        topic="billz.push_sale",
        payload={"order_id": 10778, "purchase_id": 2},
        dedupe_key="b:2",
        status="done",
    )
    report = run(BILLZ_MODE="live")
    assert check(report, "k1_billz_sale_unrecorded") == {
        "count": 2,
        "ids": sorted([unrecorded.order_id, no_sale_id.order_id]),
    }
    # the backlog: everything paid before the look-back, however old (the two checks never overlap)
    assert check(report, "k1_billz_sale_unrecorded_backlog") == {
        "count": 2,
        "ids": sorted([backlog.order_id, ancient.order_id]),
    }
    assert check(report, "k1_billz_sale_dead") == {"count": 1, "ids": [10777]}
    fake = run(BILLZ_MODE="fake")["checks"]
    assert fake["k1_billz_sale_unrecorded"]["skipped"] == "billz_not_live"
    assert fake["k1_billz_sale_unrecorded_backlog"] == {"count": 0, "ids": [], "skipped": "billz_not_live"}


def test_k2_stuck_pending_payments_and_click_payments_without_paydoc() -> None:
    stuck = PaymentFactory.create(pending=True, provider_txn_id="CLK-1", order__created_at=ago(hours=2))
    PaymentFactory.create(pending=True, order__created_at=ago(hours=2))  # no transaction attached
    PaymentFactory.create(
        pending=True, provider_txn_id="CLK-2", order__created_at=ago(minutes=50)
    )  # in grace
    no_paydoc = PaymentFactory.create(order__created_at=ago(hours=1))
    PaymentFactory.create(order__created_at=ago(hours=1), provider_state={"click_paydoc_id": "88213"})
    PaymentFactory.create(order__created_at=ago(hours=1), order__payme=True)  # Payme has no paydoc
    PaymentFactory.create(order__created_at=ago(days=9))  # outside the look-back
    empty = PaymentFactory.create(order__created_at=ago(hours=1), provider_state={"click_paydoc_id": ""})
    report = run()
    assert check(report, "k2_payment_pending_stuck") == {"count": 1, "ids": [stuck.pk]}
    assert check(report, "k2_click_paid_without_paydoc") == {"count": 2, "ids": [no_paydoc.pk, empty.pk]}


def test_k3_open_and_failed_refunds() -> None:
    old_open = RefundFactory.create(payment__order__created_at=ago(days=2))
    RefundFactory.create(payment__order__created_at=ago(hours=3))  # open for 2 h only
    old_unknown = RefundFactory.create(unknown=True, payment__order__created_at=ago(days=3))
    failed = RefundFactory.create(failed=True, payment__order__created_at=ago(hours=3))
    retried = PaymentFactory.create(order__created_at=ago(days=2), provider_state={"click_paydoc_id": "1"})
    RefundFactory.create(failed=True, payment=retried)
    RefundFactory.create(succeeded=True, payment=retried)  # a later refund returned the money
    refunded = PaymentFactory.create(refunded=True, order__created_at=ago(days=2))
    RefundFactory.create(failed=True, payment=refunded)
    report = run()
    assert check(report, "k3_refund_open") == {"count": 2, "ids": [old_open.pk, old_unknown.pk]}
    assert check(report, "k3_refund_failed") == {"count": 1, "ids": [failed.pk]}


def test_k4_paid_orders_without_a_parcel_and_parcels_without_news() -> None:
    no_row = OrderFactory.create(paid=True, created_at=ago(hours=2))
    not_created = OrderFactory.create(paid=True, created_at=ago(hours=2))
    ShipmentFactory.create(order=not_created)
    failed = OrderFactory.create(paid=True, created_at=ago(hours=3))
    ShipmentFactory.create(failed=True, order=failed)
    OrderFactory.create(paid=True, created_at=ago(minutes=40))  # paid 37 min ago
    OrderFactory.create(paid=True, pickup=True, created_at=ago(hours=2))
    shipped = OrderFactory.create(paid=True, created_at=ago(hours=5))
    ShipmentFactory.create(created=True, order=shipped, last_status_at=ago(hours=1))
    stale = OrderFactory.create(handed=True, created_at=ago(days=4))
    ShipmentFactory.create(created=True, order=stale)  # last status = creation, ~4 days ago
    never_tracked = OrderFactory.create(paid=True, created_at=ago(days=3))
    ShipmentFactory.create(created=True, order=never_tracked, last_status_at=None)
    delivered = OrderFactory.create(delivered=True, created_at=ago(days=5))
    ShipmentFactory.create(created=True, order=delivered)
    report = run()
    assert check(report, "k4_shipment_missing") == {
        "count": 3,
        "ids": sorted([no_row.pk, not_created.pk, failed.pk]),
    }
    assert check(report, "k4_parcel_untracked") == {"count": 2, "ids": sorted([stale.pk, never_tracked.pk])}


def test_k5_late_captures_whose_money_is_not_back() -> None:
    late = PaymentFactory.create(error_code="late_capture", order__created_at=ago(days=2))
    PaymentFactory.create(error_code="late_capture", order__created_at=ago(hours=2))  # captured 2 h ago
    back = PaymentFactory.create(error_code="late_capture", order__created_at=ago(days=2))
    RefundFactory.create(succeeded=True, payment=back, origin="late_capture")
    PaymentFactory.create(error_code="late_capture", refunded=True, order__created_at=ago(days=2))
    report = run()
    assert check(report, "k5_late_capture_unrefunded") == {"count": 1, "ids": [late.pk]}


def outbox_row(key: str, status: str, *, topic: str = "push.order_status", **times: datetime | None) -> int:
    row = OutboxMessage.objects.create(topic=topic, payload={}, dedupe_key=key, status=status)
    OutboxMessage.objects.filter(pk=row.pk).update(**times)
    return row.pk


def test_k6_dead_overdue_and_deferred_messages() -> None:
    dead = outbox_row("dead:new", "dead", updated_at=ago(hours=2))
    outbox_row("dead:old", "dead", updated_at=ago(days=2))
    pending = outbox_row("pending:late", "pending", available_at=ago(hours=2))
    outbox_row("pending:fresh", "pending", available_at=ago(minutes=10))
    failed = outbox_row("failed:late", "failed", available_at=ago(minutes=61))
    in_flight = outbox_row("in_flight:late", "in_flight", available_at=ago(hours=3))
    outbox_row("done:old", "done", available_at=ago(days=3))
    # deferred: judged by their window only (billz.push_sale: BILLZ_SHIFT_RETRY_MAX_HOURS 36 + 1 h grace)
    outbox_row(
        "billz:30h",
        "failed",
        topic="billz.push_sale",
        deferred_since=ago(hours=30),
        available_at=ago(hours=2),
    )
    billz = outbox_row("billz:40h", "failed", topic="billz.push_sale", deferred_since=ago(hours=40))
    other = outbox_row("other:26h", "failed", topic="x.retry", deferred_since=ago(hours=26))
    outbox_row("other:20h", "pending", topic="x.retry", deferred_since=ago(hours=20))
    outbox_row("billz:done", "done", topic="billz.push_sale", deferred_since=ago(hours=50))
    report = run()
    assert check(report, "k6_outbox_dead") == {"count": 1, "ids": [dead]}
    assert check(report, "k6_outbox_overdue") == {"count": 3, "ids": [pending, failed, in_flight]}
    assert check(report, "k6_outbox_deferred_past_window") == {"count": 2, "ids": [billz, other]}


def test_mismatches_log_one_error_line_and_raise_one_alert(caplog: pytest.LogCaptureFixture) -> None:
    for n in range(3):
        outbox_row(f"dead:{n}", "dead", updated_at=ago(hours=1))
    with caplog.at_level(logging.ERROR, logger="glowderma.reporting.tasks"):
        report = run(ALERT_WEBHOOK_URL="https://alerts.example.uz/hook", CONSISTENCY_SAMPLE_SIZE=2)
    dead = check(report, "k6_outbox_dead")
    assert dead["count"] == 3 and len(dead["ids"]) == 2  # the sample is capped, the count is not
    assert report["mismatches"] == {"k6_outbox_dead": 3}
    (line,) = [r.getMessage() for r in caplog.records if "report.consistency_mismatch" in r.getMessage()]
    assert json.loads(line.removeprefix("report.consistency_mismatch ")) == {"k6_outbox_dead": dead}
    (alert,) = OutboxMessage.objects.filter(topic=alerts.ALERT_TOPIC)
    assert alert.payload["event"] == "report.consistency_mismatch"
    assert alert.payload["fields"] == {"counts": {"k6_outbox_dead": 3}}
    entry = AuditEntry.objects.get(action="report.consistency")
    assert (entry.after or {})["mismatches"] == {"k6_outbox_dead": 3}


def test_a_failing_check_is_reported_and_never_stops_the_others(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def boom(*args: Any, **kwargs: Any) -> Any:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1/0")  # a database error inside the check's savepoint

    monkeypatch.setattr(payment_selectors, "open_refunds", boom)
    monkeypatch.setattr(loyalty_tasks, "reconcile_balances_now", boom)
    dead = outbox_row("dead:1", "dead", updated_at=ago(hours=1))
    report = run()
    assert check(report, "k3_refund_open") == {"count": 0, "ids": [], "error": "failed"}
    assert check(report, "loyalty_reconciliation") == {
        "count": 0,
        "ids": [],
        "error": "reconciliation_failed",
    }
    assert check(report, "k6_outbox_dead") == {"count": 1, "ids": [dead]}  # later checks still ran
    assert report["mismatches"] == {"k3_refund_open": 0, "k6_outbox_dead": 1, "loyalty_reconciliation": 0}
    assert "report.consistency_check_failed check=k3_refund_open" in caplog.text
    assert AuditEntry.objects.filter(action="report.consistency").count() == 1


def test_each_check_runs_under_a_statement_timeout() -> None:
    def show() -> str:
        with connection.cursor() as cursor:
            cursor.execute("SHOW statement_timeout")
            return str(cursor.fetchone()[0])

    assert tasks._guarded("probe", show) == "1min"


def show_timeout() -> str:
    with connection.cursor() as cursor:
        cursor.execute("SHOW statement_timeout")
        return str(cursor.fetchone()[0])


def test_the_statement_timeout_is_restored_after_each_check() -> None:
    # set_config(..., true) outlives a released savepoint: the caller's own transaction gets its value back
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL statement_timeout = '5s'")
        assert tasks._guarded("probe", show_timeout) == "1min"
        assert show_timeout() == "5s"
        assert tasks._guarded("probe", show_timeout, timeout_ms=tasks.LOYALTY_TIMEOUT_MS) == "4min"
        assert show_timeout() == "5s"


def test_the_ledger_reconciliation_has_its_own_time_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def reconcile() -> dict[str, Any]:
        seen.append(show_timeout())
        return {"mismatches": 0, "checks": {}, "negative_balances": 0}

    monkeypatch.setattr(loyalty_tasks, "reconcile_balances_now", reconcile)
    run()
    assert seen == ["4min"]


def test_the_soft_time_limit_stops_the_checks_but_the_report_is_still_written(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    reconciled: list[bool] = []

    def out_of_time(*args: Any, **kwargs: Any) -> Any:
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(payment_selectors, "open_refunds", out_of_time)
    monkeypatch.setattr(loyalty_tasks, "reconcile_balances_now", lambda: reconciled.append(True))
    with caplog.at_level(logging.ERROR, logger="glowderma.reporting.tasks"):
        report = run(ALERT_WEBHOOK_URL="https://alerts.example.uz/hook")
    names = list(report["checks"])
    assert names == CHECKS  # every check is listed, in order
    stopped = names[names.index("k3_refund_open") :]
    for name in names:
        expected_error = "time_limit" if name in stopped else None
        assert report["checks"][name].get("error") == expected_error, name
    assert reconciled == []  # nothing started after the soft limit
    assert set(report["mismatches"]) == set(stopped)
    entry = AuditEntry.objects.get(action="report.consistency")
    assert (entry.after or {})["checks"]["loyalty_reconciliation"]["error"] == "time_limit"
    (alert,) = OutboxMessage.objects.filter(topic=alerts.ALERT_TOPIC)
    assert alert.payload["fields"]["errors"] == dict.fromkeys(stopped, "time_limit")
    assert "report.consistency_time_limit stopped=k3_refund_open,k3_refund_failed" in caplog.text
    assert "report.consistency_check_failed" not in caplog.text  # not swallowed as a check failure


def test_the_command_prints_the_report() -> None:
    outbox_row("dead:1", "dead", updated_at=ago(hours=1))
    out = StringIO()
    call_command("consistency_report", stdout=out)
    text = out.getvalue()
    assert "k1_billz_sale_unrecorded           skipped (billz_not_live)" in text
    assert "k6_outbox_dead                     1  ids: " in text
    assert "1 check(s) need attention (docs/ops/monitoring.md)." in text
    out = StringIO()
    call_command("consistency_report", "--json", stdout=out)
    assert json.loads(out.getvalue())["mismatches"] == {"k6_outbox_dead": 1}
    assert AuditEntry.objects.filter(action="report.consistency").count() == 2


def test_the_command_says_when_everything_passed(monkeypatch: pytest.MonkeyPatch) -> None:
    report = {"checks": {"k3_refund_open": {"count": 0, "ids": []}, "x": {"count": 0, "ids": [], "error": "failed"}}, "mismatches": {}}  # fmt: skip  # noqa: E501
    monkeypatch.setattr(
        "glowderma.reporting.management.commands.consistency_report.run_consistency_report", lambda: report
    )
    out = StringIO()
    call_command("consistency_report", stdout=out)
    assert "x                                  ERROR (failed)" in out.getvalue()
    assert out.getvalue().strip().endswith("All checks passed.")
