"""Nightly reconciliation of the cashback ledger (design 02 §1.9.3; beat ``loyalty.reconcile_balances``).

Each check is one SQL statement (one snapshot under READ COMMITTED), so concurrent postings cannot produce
false mismatches. Every finding is written as an audit entry ``loyalty.reconciliation_mismatch`` (actor
``system``, ``entity_type="loyalty_account"``), then a summary ``loyalty.reconciliation_completed``. Nothing
is corrected automatically: the ledger is append-only and a mismatch means a bug or a raw write, which staff
investigate.
"""

from __future__ import annotations

import logging
from typing import Any

from celery import shared_task  # type: ignore[import-untyped]
from django.db import connection

from glowderma.core import audit
from glowderma.loyalty.models import LoyaltyAccount

logger = logging.getLogger(__name__)

# check → (SQL, columns). Every row is a mismatch; ``account_id`` is the first column.
CHECKS: dict[str, tuple[str, tuple[str, ...]]] = {
    # C1: balance == Σ entries
    "ledger_sum": (
        """
        SELECT a.id, a.balance, COALESCE(SUM(e.amount), 0) AS ledger_sum
        FROM loyalty_loyaltyaccount a
        LEFT JOIN loyalty_ledgerentry e ON e.account_id = a.id
        GROUP BY a.id, a.balance
        HAVING a.balance <> COALESCE(SUM(e.amount), 0)
        ORDER BY a.id
        """,
        ("account_id", "balance", "ledger_sum"),
    ),
    # C2: the newest entry's balance_after == balance (a balance change without an entry)
    "last_balance_after": (
        """
        SELECT a.id, a.balance, last.balance_after
        FROM loyalty_loyaltyaccount a
        JOIN LATERAL (SELECT e.balance_after FROM loyalty_ledgerentry e
                      WHERE e.account_id = a.id ORDER BY e.at DESC, e.id DESC LIMIT 1) last ON true
        WHERE last.balance_after <> a.balance
        ORDER BY a.id
        """,
        ("account_id", "balance", "last_balance_after"),
    ),
    # C3: the running chain is consistent (balance_after_i == balance_after_{i-1} + amount_i)
    "chain": (
        """
        SELECT account_id, id, amount, balance_after, prev_after FROM (
          SELECT e.id, e.account_id, e.amount, e.balance_after,
                 LAG(e.balance_after, 1, 0) OVER (PARTITION BY e.account_id ORDER BY e.at, e.id) AS prev_after
          FROM loyalty_ledgerentry e) x
        WHERE balance_after <> prev_after + amount
        ORDER BY account_id, id
        """,
        ("account_id", "entry_id", "amount", "balance_after", "previous_balance_after"),
    ),
    # C5: purchase ↔ entry links
    "purchase_links": (
        """
        SELECT p.customer_id, p.id, p.cashback_amount, c.amount, p.credited_entry_id, p.reversal_entry_id
        FROM loyalty_purchase p
        LEFT JOIN loyalty_ledgerentry c ON c.id = p.credited_entry_id
        LEFT JOIN loyalty_ledgerentry r ON r.id = p.reversal_entry_id
        WHERE (p.cashback_amount > 0 AND (c.amount <> p.cashback_amount OR c.purchase_id <> p.id))
           OR (p.reversed AND p.credited_entry_id IS NOT NULL
               AND (r.id IS NULL OR r.reverses_id <> p.credited_entry_id))
        ORDER BY p.id
        """,
        (
            "customer_id",
            "purchase_id",
            "cashback_amount",
            "credited_amount",
            "credited_entry_id",
            "reversal_entry_id",
        ),
    ),
    # C6: captured holds ↔ debit entries
    "captured_holds": (
        """
        SELECT h.account_id, h.id, h.amount, d.kind, d.amount, d.order_id, h.order_id
        FROM loyalty_cashbackhold h JOIN loyalty_ledgerentry d ON d.id = h.captured_entry_id
        WHERE d.kind <> 'debit' OR d.amount <> -h.amount OR d.order_id <> h.order_id
        ORDER BY h.id
        """,
        ("account_id", "hold_id", "hold_amount", "entry_kind", "entry_amount", "entry_order_id", "order_id"),
    ),
}

NEGATIVE_BALANCES_SQL = "SELECT COUNT(*) FROM loyalty_loyaltyaccount WHERE balance < 0"


def _run(sql: str) -> list[tuple[Any, ...]]:
    with connection.cursor() as cursor:
        cursor.execute(sql)
        return [tuple(row) for row in cursor.fetchall()]


def reconcile_balances_now() -> dict[str, Any]:
    """Run every check once; audit each mismatch and a summary; return the counts (never fixes anything)."""
    mismatches = 0
    per_check: dict[str, int] = {}
    for check, (sql, columns) in CHECKS.items():
        rows = _run(sql)
        per_check[check] = len(rows)
        for row in rows:
            finding = dict(zip(columns, row, strict=True))
            entity_id = finding.pop("account_id") if "account_id" in finding else finding.pop("customer_id")
            audit.record(
                action="loyalty.reconciliation_mismatch",
                entity_type="loyalty_account" if "account_id" in columns else "customer",
                entity_id=str(entity_id),
                after={"check": check, **finding},
            )
            logger.warning("Loyalty reconciliation mismatch check=%s %s", check, finding)
        mismatches += len(rows)
    negative = int(_run(NEGATIVE_BALANCES_SQL)[0][0])
    summary = {
        "accounts": LoyaltyAccount.objects.count(),
        "mismatches": mismatches,
        "negative_balances": negative,
        "checks": per_check,
    }
    audit.record(
        action="loyalty.reconciliation_completed", entity_type="loyalty", entity_id="ledger", after=summary
    )
    return summary


@shared_task(name="loyalty.reconcile_balances")  # type: ignore[untyped-decorator]
def reconcile_balances() -> dict[str, Any]:
    return reconcile_balances_now()
