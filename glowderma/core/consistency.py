"""Building blocks of the nightly consistency report (``reporting.consistency_report``,
docs/ops/monitoring.md).

Every check is one read-only statement that returns how many rows are wrong and the ids of the first few:
:func:`finding` adds ``COUNT(*) OVER ()`` to the check's queryset and keeps ``limit`` ids in id order, so the
count and the sample come from the same snapshot. The modules own their checks (selectors); this module holds
the shared result type and the outbox checks of K6.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.db.models import Count, Min, Q, QuerySet, Window

from glowderma.core import outbox
from glowderma.core.models import OutboxMessage

# Outbox statuses that still expect a delivery (``failed`` = waiting for its retry).
OPEN_OUTBOX_STATUSES: tuple[str, ...] = ("pending", "failed", "in_flight")


@dataclass(frozen=True, slots=True)
class Finding:
    """The outcome of one check: how many rows are wrong and up to N of their ids (ids only, never PII)."""

    count: int
    ids: list[int] = field(default_factory=list)
    skipped: str = ""  # why the check did not run (e.g. "billz_not_live"); "" = it ran

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"count": self.count, "ids": list(self.ids)}
        if self.skipped:
            data["skipped"] = self.skipped
        return data


def finding(queryset: QuerySet[Any], *, limit: int, id_field: str = "pk") -> Finding:
    """``queryset``'s row count and its first ``limit`` values of ``id_field`` (ascending), one statement."""
    rows = list(
        queryset.annotate(_finding_total=Window(expression=Count("*")))
        .order_by(id_field)
        .values_list(id_field, "_finding_total")[: max(limit, 1)]
    )
    if not rows:
        return Finding(count=0)
    return Finding(count=int(rows[0][1]), ids=[int(row[0]) for row in rows])


# --- K6: outbox -------------------------------------------------------------------------------------------
# Plan (all three): the partial index ``outbox_unfinished_idx`` (available_at, id) WHERE NOT status = 'done'
# (core.0010) holds only the rows that are not done — open messages and dead ones, which retention never
# purges (core.retention) — so no check reads the done rows that make up nearly the whole table; the status
# (and the topic) are filters over those few rows, ``available_at`` a range where the check has one.


def dead_messages(since: datetime, *, limit: int, topic: str | None = None) -> Finding:
    """Messages that went ``dead`` at or after ``since`` (their ``updated_at``), of ``topic`` or of any."""
    queryset = OutboxMessage.objects.filter(status="dead", updated_at__gte=since)
    if topic is not None:
        queryset = queryset.filter(topic=topic)
    return finding(queryset, limit=limit)


def dead_count() -> int:
    """Every ``dead`` message (an action item until requeued): the ops console's sidebar badge. Reads the
    partial ``outbox_unfinished_idx`` rows only."""
    return OutboxMessage.objects.filter(status="dead").count()


def overdue_messages(now: datetime, overdue: timedelta, *, limit: int) -> Finding:
    """Messages still open (pending, failed or in flight) although they were due more than ``overdue`` ago:
    the dispatcher is not running or a message is wedged. Deferred messages (``RetryLater``) are judged by
    :func:`deferred_past_window` instead."""
    queryset = OutboxMessage.objects.filter(
        status__in=OPEN_OUTBOX_STATUSES, available_at__lt=now - overdue, deferred_since__isnull=True
    )
    return finding(queryset, limit=limit)


def deferred_past_window(now: datetime, grace: timedelta, *, limit: int) -> Finding:
    """Open messages deferred for longer than their topic's window (:func:`outbox.deferral_windows`, else
    ``OUTBOX_DEFERRAL_WINDOW_HOURS``) plus ``grace``: their handler should have given up and buried them."""
    windows = outbox.deferral_windows()
    default = timedelta(hours=int(settings.OUTBOX_DEFERRAL_WINDOW_HOURS))
    past = Q(deferred_since__lt=now - default - grace) & ~Q(topic__in=list(windows))
    for topic, window in windows.items():
        past |= Q(topic=topic, deferred_since__lt=now - window - grace)
    queryset = OutboxMessage.objects.filter(status__in=OPEN_OUTBOX_STATUSES, deferred_since__isnull=False)
    return finding(queryset.filter(past), limit=limit)


@dataclass(frozen=True, slots=True)
class TopicBacklog:
    """The unfinished outbox rows of one topic (the ops console's outbox table)."""

    topic: str
    open: int  # pending, failed (waiting for a retry) or in flight
    dead: int
    overdue: int  # open, not deferred, due more than ``overdue`` ago (K6)
    deferred: int  # open and deferred by its handler (``RetryLater``)
    oldest_due: datetime | None  # the earliest ``available_at`` of the open rows not deferred


def backlog_by_topic(now: datetime, overdue: timedelta) -> list[TopicBacklog]:
    """Every topic with an unfinished row (open or dead), worst first (dead, then overdue, then open).

    One statement over the partial ``outbox_unfinished_idx`` (NOT status = 'done'): the done rows that make up
    nearly the whole table are never read."""
    open_q = Q(status__in=OPEN_OUTBOX_STATUSES)
    rows = (
        OutboxMessage.objects.exclude(status="done")
        .values("topic")
        .annotate(
            open=Count("id", filter=open_q),
            dead=Count("id", filter=Q(status="dead")),
            overdue=Count(
                "id", filter=open_q & Q(available_at__lt=now - overdue, deferred_since__isnull=True)
            ),
            deferred=Count("id", filter=open_q & Q(deferred_since__isnull=False)),
            oldest_due=Min("available_at", filter=open_q & Q(deferred_since__isnull=True)),
        )
        .order_by()
    )
    backlog = [
        TopicBacklog(
            topic=str(row["topic"]),
            open=int(row["open"]),
            dead=int(row["dead"]),
            overdue=int(row["overdue"]),
            deferred=int(row["deferred"]),
            oldest_due=row["oldest_due"],
        )
        for row in rows
    ]
    return sorted(backlog, key=lambda row: (-row.dead, -row.overdue, -row.open, row.topic))


__all__ = [
    "OPEN_OUTBOX_STATUSES",
    "Finding",
    "TopicBacklog",
    "backlog_by_topic",
    "dead_count",
    "dead_messages",
    "deferred_past_window",
    "finding",
    "overdue_messages",
]
