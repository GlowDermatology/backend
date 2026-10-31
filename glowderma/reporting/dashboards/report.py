"""The latest nightly consistency report, read from its audit row (``report.consistency``, written by
:func:`glowderma.reporting.tasks.run_consistency_report`) — never run on a page load.

Audit shape: ``after = {"checks": {name: {"count", "ids", ["skipped"], ["error"], …}}, "mismatches":
{name: count}}``, ``created_at`` = when it ran. The ledger reconciliation (``loyalty_reconciliation``)
carries ``checks`` and ``negative_balances`` of its own and names no ids.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from glowderma.core.models import AuditEntry
from glowderma.reporting import tasks
from glowderma.reporting.dashboards import CRITICAL, OK, WARN, by_age, worst

REPORT_ACTION = "report.consistency"
RECONCILE_ACTION = "loyalty.reconciliation_completed"
RECONCILE_MISMATCH_ACTION = "loyalty.reconciliation_mismatch"
# The report runs nightly (03:40 Tashkent): older than this, a night was missed; after three nights it is
# critical. The report and the ledger reconciliation (which runs with it) are judged alike on every page.
REPORT_STALE_AFTER = timedelta(hours=36)
REPORT_CRITICAL_AFTER = timedelta(hours=72)
LOYALTY_CHECK = tasks.LOYALTY_CHECK


@dataclass(frozen=True, slots=True)
class CheckInfo:
    code: str
    label: str
    model: str  # the admin model its ids name ("" = no ids)
    severity: str  # the state of a non-zero count


# The nightly checks (glowderma.reporting.tasks) as the dashboards name and judge them.
CHECKS: dict[str, CheckInfo] = {
    "k1_billz_sale_unrecorded": CheckInfo(
        "K1", "Billz sotuvi yozilmagan (oxirgi kunlar)", "commerce_order", WARN
    ),
    "k1_billz_sale_unrecorded_backlog": CheckInfo(
        "K1", "Billz sotuvi yozilmagan (eski)", "commerce_order", WARN
    ),
    "k1_billz_sale_dead": CheckInfo("K1", "Billz sotuvi: o‘lik xabar", "commerce_order", CRITICAL),
    "k2_payment_pending_stuck": CheckInfo(
        "K2", "Osilib qolgan to‘lovlar (pending)", "payments_payment", CRITICAL
    ),
    "k2_click_paid_without_paydoc": CheckInfo(
        "K2", "Click: paydoc’siz to‘langan to‘lovlar", "payments_payment", CRITICAL
    ),
    "k3_refund_open": CheckInfo("K3", "Uzoq ochiq turgan qaytarishlar", "payments_refund", WARN),
    "k3_refund_failed": CheckInfo(
        "K3", "Muvaffaqiyatsiz qaytarishlar (harakat kerak)", "payments_refund", CRITICAL
    ),
    "k4_shipment_missing": CheckInfo(
        "K4", "To‘langan, lekin jo‘natilmagan buyurtmalar", "commerce_order", WARN
    ),
    "k4_parcel_untracked": CheckInfo("K4", "Kuzatilmayotgan posilkalar", "commerce_order", WARN),
    "k5_late_capture_unrefunded": CheckInfo(
        "K5", "Kech yechilgan, qaytarilmagan pullar", "payments_payment", CRITICAL
    ),
    "k6_outbox_dead": CheckInfo("K6", "O‘lik outbox xabarlari (24 soat)", "core_outboxmessage", WARN),
    "k6_outbox_overdue": CheckInfo("K6", "Kechikkan outbox xabarlari", "core_outboxmessage", WARN),
    "k6_outbox_deferred_past_window": CheckInfo(
        "K6", "Muddati o‘tgan kechiktirilgan xabarlar", "core_outboxmessage", WARN
    ),
    LOYALTY_CHECK: CheckInfo("L", "Cashback daftari solishtiruvi", "", CRITICAL),
}


@dataclass(frozen=True, slots=True)
class ReportCheck:
    name: str
    count: int
    ids: list[int]
    skipped: str = ""
    error: str = ""


@dataclass(frozen=True, slots=True)
class ReportSummary:
    entry_id: int
    ran_at: datetime
    checks: dict[str, ReportCheck] = field(default_factory=dict)

    @property
    def findings(self) -> int:
        return sum(check.count for check in self.checks.values())

    @property
    def failing(self) -> list[str]:
        """Checks with a count or an error, in report order."""
        return [name for name, check in self.checks.items() if check.count or check.error]

    @property
    def errors(self) -> list[str]:
        return [name for name, check in self.checks.items() if check.error]

    def stale(self, now: datetime) -> bool:
        return now - self.ran_at > REPORT_STALE_AFTER


def age_state(ran_at: datetime | None, now: datetime) -> str:
    """How fresh a nightly trace is (the report or the ledger reconciliation): ``critical`` when it never
    ran or is older than :data:`REPORT_CRITICAL_AFTER`, ``warn`` past :data:`REPORT_STALE_AFTER`."""
    return by_age(
        None if ran_at is None else now - ran_at, warn=REPORT_STALE_AFTER, critical=REPORT_CRITICAL_AFTER
    )


def findings_state(summary: ReportSummary) -> str:
    """The state of a report's findings: the worst :data:`CHECKS` severity among the checks that found rows
    (an unknown check warns), ``critical`` for a check that errored — never a count of failing checks."""
    states = [OK]
    for name, check in summary.checks.items():
        if check.error:
            states.append(CRITICAL)
        elif check.count:
            info = CHECKS.get(name)
            states.append(info.severity if info is not None else WARN)
    return worst(states)


def _ids(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    return [int(item) for item in value if isinstance(item, int) and not isinstance(item, bool)]


def summary(entry: AuditEntry | None) -> ReportSummary | None:
    """The report of ``entry`` (``None`` without one); a malformed check reads as an error, never raises."""
    if entry is None:
        return None
    after = entry.after if isinstance(entry.after, dict) else {}
    raw = after.get("checks")
    if not isinstance(raw, dict):
        raw = {}
    checks: dict[str, ReportCheck] = {}
    for name, data in raw.items():
        if not isinstance(data, dict):
            checks[str(name)] = ReportCheck(str(name), 0, [], error="malformed")
            continue
        count = data.get("count")
        checks[str(name)] = ReportCheck(
            name=str(name),
            count=int(count) if isinstance(count, int) else 0,
            ids=_ids(data.get("ids")),
            skipped=str(data.get("skipped") or ""),
            error=str(data.get("error") or ""),
        )
    return ReportSummary(entry_id=int(entry.pk), ran_at=entry.created_at, checks=checks)


__all__ = [
    "CHECKS",
    "LOYALTY_CHECK",
    "REPORT_CRITICAL_AFTER",
    "CheckInfo",
    "RECONCILE_ACTION",
    "RECONCILE_MISMATCH_ACTION",
    "REPORT_ACTION",
    "REPORT_STALE_AFTER",
    "ReportCheck",
    "ReportSummary",
    "age_state",
    "findings_state",
    "summary",
]
