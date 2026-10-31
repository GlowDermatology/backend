"""``core.consistency.backlog_by_topic``: the ops console's outbox table (one statement, done rows unread)."""

from datetime import datetime, timedelta
from typing import Any

import pytest

from glowderma.core import consistency
from glowderma.core.models import OutboxMessage
from glowderma.core.timeutils import TASHKENT

pytestmark = pytest.mark.django_db
NOW = datetime(2026, 10, 31, 12, 0, tzinfo=TASHKENT)
OVERDUE = timedelta(minutes=60)


def _message(topic: str, status: str, due: datetime, **extra: Any) -> OutboxMessage:
    return OutboxMessage.objects.create(
        topic=topic,
        payload={},
        dedupe_key=f"{topic}:{status}:{due.isoformat()}:{extra}",
        status=status,
        available_at=due,
        **extra,
    )


def test_backlog_counts_by_topic_worst_first(django_assert_num_queries: Any) -> None:
    _message("sms.send", "pending", NOW - timedelta(minutes=5))
    _message("sms.send", "failed", NOW - timedelta(minutes=90))  # overdue
    _message("sms.send", "in_flight", NOW - timedelta(minutes=61))  # overdue
    _message("billz.push_sale", "dead", NOW - timedelta(days=3))
    _message("billz.push_sale", "pending", NOW - timedelta(hours=5), deferred_since=NOW - timedelta(hours=5))
    _message("billz.push_sale", "done", NOW - timedelta(days=1))
    _message("push.order_status", "done", NOW - timedelta(days=1))  # a topic with only done rows: absent
    with django_assert_num_queries(1):
        rows = consistency.backlog_by_topic(NOW, OVERDUE)
    assert [row.topic for row in rows] == ["billz.push_sale", "sms.send"]
    billz, sms = rows
    assert (billz.open, billz.dead, billz.overdue, billz.deferred) == (1, 1, 0, 1)
    assert billz.oldest_due is None  # the deferred row is judged by its window, not as a stalled dispatch
    assert (sms.open, sms.dead, sms.overdue, sms.deferred) == (3, 0, 2, 0)
    assert sms.oldest_due == NOW - timedelta(minutes=90)


def test_backlog_is_empty_without_unfinished_rows() -> None:
    _message("sms.send", "done", NOW)
    assert consistency.backlog_by_topic(NOW, OVERDUE) == []
