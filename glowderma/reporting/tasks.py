"""Nightly consistency report (beat ``reporting-consistency-report``, 03:40 Tashkent; ``manage.py
consistency_report [--json]``; docs/ops/monitoring.md).

Read-only checks, each one statement over an index or a bounded range (the owning selector names its plan),
each returning how many rows are wrong and the first ``CONSISTENCY_SAMPLE_SIZE`` ids:

* **K1** paid orders whose Billz sale is not recorded ``CONSISTENCY_BILLZ_SALE_MINUTES`` after payment while
  Billz is live (paid within the look-back ``CONSISTENCY_LOOKBACK_DAYS``, and — ``_backlog`` — paid before it,
  however old), and dead ``billz.push_sale`` messages of any age (order ids);
* **K2** ``pending`` payments holding a transaction past ``reservation_expires_at`` +
  ``PAYMENT_IN_FLIGHT_GRACE_SECONDS`` + ``CONSISTENCY_PAYMENT_PENDING_MINUTES``, and Click payments ``paid``
  without ``click_paydoc_id`` (look-back; payment ids);
* **K3** refunds open for more than ``CONSISTENCY_REFUND_OPEN_HOURS``, and ``failed`` refunds still needing
  staff (the dashboard rule; refund ids);
* **K4** delivery orders ``processing`` without a BTS parcel ``CONSISTENCY_SHIPMENT_MINUTES`` after payment,
  and parcels with no new BTS status for ``CONSISTENCY_PARCEL_UNTRACKED_HOURS`` (order ids);
* **K5** refused (late) captures without a succeeded refund after ``CONSISTENCY_LATE_CAPTURE_HOURS``
  (payment ids);
* **K6** outbox messages dead in the last ``CONSISTENCY_OUTBOX_DEAD_HOURS``, open messages overdue by
  ``CONSISTENCY_OUTBOX_OVERDUE_MINUTES`` (deferred ones excluded), and deferred messages past their window
  (message ids);
* the loyalty ledger reconciliation (``loyalty.tasks.reconcile_balances_now``, called here — it writes its
  own audit rows; the report keeps its counts).

Outcome: the audit row ``report.consistency {checks, mismatches}`` (actor ``system``); when a count is not
zero or a check failed, an ERROR line ``report.consistency_mismatch`` with the non-zero checks (counts and
ids, never personal data) and the alert ``report.consistency_mismatch`` (counts). A check that fails is
reported with ``error`` and never stops the others.

Time bounds: every check statement runs under a transaction-local ``statement_timeout``
(:data:`CHECK_TIMEOUT_MS`; the ledger reconciliation :data:`LOYALTY_TIMEOUT_MS`), restored to the previous
value once the check is done, so a caller's own transaction (``manage.py`` inside a test, a shell) keeps its
setting. When the Celery soft time limit fires (``SoftTimeLimitExceeded``), the check that was running and
every one after it are reported with ``error: time_limit`` and no further check starts; the audit row, the
ERROR line and the alert are still written before the hard limit.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from functools import partial
from typing import Any

from celery import shared_task  # type: ignore[import-untyped]
from celery.exceptions import SoftTimeLimitExceeded  # type: ignore[import-untyped]
from django.conf import settings
from django.db import connection, transaction

from glowderma.commerce import selectors as commerce_selectors
from glowderma.core import alerts, audit, consistency, integration_modes, timeutils
from glowderma.core.consistency import Finding
from glowderma.delivery import selectors as delivery_selectors
from glowderma.loyalty import tasks as loyalty_tasks
from glowderma.payments import selectors as payment_selectors

logger = logging.getLogger(__name__)

# Every check statement is cut off after this long (a transaction-local statement_timeout in its savepoint).
CHECK_TIMEOUT_MS = 60_000
# The ledger reconciliation reads every entry by design (design 02 §1.9.3): a longer bound of its own, still
# well inside the Celery soft time limit (CELERY_TASK_SOFT_TIME_LIMIT, 540 s).
LOYALTY_TIMEOUT_MS = 240_000
LOYALTY_CHECK = "loyalty_reconciliation"
BILLZ_NOT_LIVE = "billz_not_live"
TIME_LIMIT = "time_limit"

Check = Callable[[], Finding]


def _checks(now: datetime) -> list[tuple[str, Check]]:
    limit = int(settings.CONSISTENCY_SAMPLE_SIZE)
    lookback = now - timedelta(days=int(settings.CONSISTENCY_LOOKBACK_DAYS))
    grace = timedelta(seconds=int(settings.PAYMENT_IN_FLIGHT_GRACE_SECONDS))
    overdue = timedelta(minutes=int(settings.CONSISTENCY_OUTBOX_OVERDUE_MINUTES))
    billz_live = integration_modes.provider_mode("billz") == "live"

    def k1_unrecorded() -> Finding:
        if not billz_live:
            return Finding(count=0, skipped=BILLZ_NOT_LIVE)
        paid_before = now - timedelta(minutes=int(settings.CONSISTENCY_BILLZ_SALE_MINUTES))
        return commerce_selectors.unrecorded_billz_sales(lookback, paid_before, limit=limit)

    def k1_unrecorded_backlog() -> Finding:
        if not billz_live:
            return Finding(count=0, skipped=BILLZ_NOT_LIVE)
        return commerce_selectors.unrecorded_billz_sales_before(lookback, limit=limit)

    return [
        ("k1_billz_sale_unrecorded", k1_unrecorded),
        ("k1_billz_sale_unrecorded_backlog", k1_unrecorded_backlog),
        ("k1_billz_sale_dead", lambda: commerce_selectors.dead_billz_sales(limit=limit)),
        (
            "k2_payment_pending_stuck",
            lambda: payment_selectors.stuck_pending_payments(
                now - grace - timedelta(minutes=int(settings.CONSISTENCY_PAYMENT_PENDING_MINUTES)),
                limit=limit,
            ),
        ),
        (
            "k2_click_paid_without_paydoc",
            lambda: payment_selectors.click_paid_without_paydoc(lookback, limit=limit),
        ),
        (
            "k3_refund_open",
            lambda: payment_selectors.open_refunds(
                now - timedelta(hours=int(settings.CONSISTENCY_REFUND_OPEN_HOURS)), limit=limit
            ),
        ),
        ("k3_refund_failed", lambda: payment_selectors.failed_refunds_needing_action(limit=limit)),
        (
            "k4_shipment_missing",
            lambda: commerce_selectors.unshipped_paid_orders(
                now - timedelta(minutes=int(settings.CONSISTENCY_SHIPMENT_MINUTES)), limit=limit
            ),
        ),
        (
            "k4_parcel_untracked",
            lambda: delivery_selectors.untracked_parcels(
                now - timedelta(hours=int(settings.CONSISTENCY_PARCEL_UNTRACKED_HOURS)), limit=limit
            ),
        ),
        (
            "k5_late_capture_unrefunded",
            lambda: payment_selectors.late_captures_not_refunded(
                now - timedelta(hours=int(settings.CONSISTENCY_LATE_CAPTURE_HOURS)), limit=limit
            ),
        ),
        (
            "k6_outbox_dead",
            lambda: consistency.dead_messages(
                now - timedelta(hours=int(settings.CONSISTENCY_OUTBOX_DEAD_HOURS)), limit=limit
            ),
        ),
        ("k6_outbox_overdue", lambda: consistency.overdue_messages(now, overdue, limit=limit)),
        (
            "k6_outbox_deferred_past_window",
            lambda: consistency.deferred_past_window(now, overdue, limit=limit),
        ),
    ]


def _guarded[T](name: str, run: Callable[[], T], *, timeout_ms: int = CHECK_TIMEOUT_MS) -> T | None:
    """Run one check in its own savepoint (a failure never poisons the caller's transaction), its statements
    cut off after ``timeout_ms``; ``None`` (logged with the traceback) when it failed.

    ``set_config(…, true)`` is transaction-local, not savepoint-local: a released savepoint would leave the
    timeout on the caller's transaction, so the previous value is put back once the check succeeded (a
    failed check's savepoint rollback reverts it already). ``SoftTimeLimitExceeded`` is not a check failure:
    it propagates so the report stops starting checks (module doc)."""
    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_setting('statement_timeout')")
                previous = str(cursor.fetchone()[0])
                cursor.execute("SELECT set_config('statement_timeout', %s, true)", [str(timeout_ms)])
            result = run()
            with connection.cursor() as cursor:
                cursor.execute("SELECT set_config('statement_timeout', %s, true)", [previous])
            return result
    except SoftTimeLimitExceeded:
        raise
    except Exception:
        logger.exception("report.consistency_check_failed check=%s", name)
        return None


def _loyalty() -> dict[str, Any]:
    summary = _guarded(LOYALTY_CHECK, loyalty_tasks.reconcile_balances_now, timeout_ms=LOYALTY_TIMEOUT_MS)
    if summary is None:
        return {"count": 0, "ids": [], "error": "reconciliation_failed"}
    return {
        "count": int(summary.get("mismatches", 0)),
        "ids": [],  # the accounts are named by the loyalty.reconciliation_mismatch audit rows
        "checks": dict(summary.get("checks", {})),
        "negative_balances": int(summary.get("negative_balances", 0)),
    }


def run_consistency_report(now: datetime | None = None) -> dict[str, Any]:
    """Run every check once, record the audit row, log and alert on mismatches; returns the report."""
    instant = now or timeutils.now()
    checks: dict[str, dict[str, Any]] = {}
    planned: list[tuple[str, Callable[[], dict[str, Any]]]] = [
        (name, partial(_check_result, name, run)) for name, run in _checks(instant)
    ]
    planned.append((LOYALTY_CHECK, _loyalty))
    for position, (name, run_check) in enumerate(planned):
        try:
            checks[name] = run_check()
        except SoftTimeLimitExceeded:
            stopped = [later for later, _run in planned[position:]]
            logger.error("report.consistency_time_limit stopped=%s", ",".join(stopped))
            # a statement cut off mid-flight can leave the connection busy: the audit needs a usable one
            if not connection.in_atomic_block:
                connection.close_if_unusable_or_obsolete()
            for later in stopped:
                checks[later] = {"count": 0, "ids": [], "error": TIME_LIMIT}
            break
    mismatches = {
        name: check["count"] for name, check in checks.items() if check["count"] or "error" in check
    }
    report = {
        "generated_at": instant.astimezone(timeutils.TASHKENT).isoformat(timespec="seconds"),
        "checks": checks,
        "mismatches": mismatches,
    }
    audit.record(
        action="report.consistency",
        entity_type="report",
        entity_id="consistency",
        after={"checks": checks, "mismatches": mismatches},
    )
    if mismatches:
        details = {
            name: {key: value for key, value in checks[name].items() if key in ("count", "ids", "error")}
            for name in mismatches
        }
        logger.error("report.consistency_mismatch %s", json.dumps(details, sort_keys=True))
        errors = {name: check["error"] for name, check in checks.items() if "error" in check}
        alerts.alert(
            "report.consistency_mismatch", {"counts": mismatches, **({"errors": errors} if errors else {})}
        )
    else:
        logger.info("report.consistency ok")
    return report


def _check_result(name: str, run: Check) -> dict[str, Any]:
    result = _guarded(name, run)
    return result.as_dict() if result is not None else {"count": 0, "ids": [], "error": "failed"}


@shared_task(name="reporting.consistency_report")  # type: ignore[untyped-decorator]
def consistency_report() -> dict[str, Any]:
    """Beat entry ``reporting-consistency-report`` (03:40 Tashkent)."""
    return run_consistency_report()


__all__ = [
    "CHECK_TIMEOUT_MS",
    "LOYALTY_CHECK",
    "LOYALTY_TIMEOUT_MS",
    "TIME_LIMIT",
    "consistency_report",
    "run_consistency_report",
]
