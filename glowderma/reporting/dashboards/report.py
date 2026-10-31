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

REPORT_ACTION = "report.consistency"
RECONCILE_ACTION = "loyalty.reconciliation_completed"
RECONCILE_MISMATCH_ACTION = "loyalty.reconciliation_mismatch"
# The report runs nightly (03:40 Tashkent): older than this, a night was missed.
REPORT_STALE_AFTER = timedelta(hours=36)


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
    "RECONCILE_ACTION",
    "RECONCILE_MISMATCH_ACTION",
    "REPORT_ACTION",
    "REPORT_STALE_AFTER",
    "ReportCheck",
    "ReportSummary",
    "summary",
]
