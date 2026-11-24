"""The customer inbox (customer contract §2.12): delivery acknowledgements and read marks."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from django.db import transaction
from django.db.models import F, Value
from django.db.models.functions import Coalesce
from rest_framework.exceptions import NotFound

from glowderma.core import timeutils
from glowderma.notifications import selectors
from glowderma.notifications.models import (
    Campaign,
    CampaignRecipient,
    InboxNotification,
)


@dataclass(frozen=True, slots=True)
class AckResult:
    acknowledged: int  # the customer's rows among the ids (all delivered now): stable under retries
    newly: int  # rows this call moved from undelivered to delivered


def _acknowledge(inbox_ids: Sequence[int], instant: Any) -> int:
    """Mark undelivered rows delivered; for each linked ``CampaignRecipient`` still undelivered set its
    ``delivered_at`` and add the number of recipient rows that **actually changed** to
    ``Campaign.delivered`` (``COALESCE(delivered, 0) + n``), so a retried acknowledgement never counts twice.

    Lock order campaign → recipient → inbox, the order of ``send_batch``'s settle step, so an ack that
    arrives while its batch is being settled waits instead of deadlocking.
    """
    if not inbox_ids:
        return 0
    campaign_ids = sorted(
        set(
            CampaignRecipient.objects.filter(inbox_id__in=inbox_ids, delivered_at__isnull=True).values_list(
                "campaign_id", flat=True
            )
        )
    )
    if campaign_ids:
        list(
            Campaign.objects.select_for_update().filter(pk__in=campaign_ids).order_by("pk").values_list("pk")
        )
    for campaign_id in campaign_ids:
        changed = CampaignRecipient.objects.filter(
            campaign_id=campaign_id, inbox_id__in=inbox_ids, delivered_at__isnull=True
        ).update(delivered_at=instant)
        if changed:
            Campaign.objects.filter(pk=campaign_id).update(
                delivered=Coalesce(F("delivered"), Value(0)) + changed, updated_at=instant
            )
    return InboxNotification.objects.filter(pk__in=inbox_ids, delivered_at__isnull=True).update(
        delivered_at=instant, updated_at=instant
    )


@transaction.atomic
def acknowledge(
    customer_id: int, inbox_ids: Iterable[int], *, horizon: Any = None, at: Any = None
) -> AckResult:
    """``POST me/notifications/ack/``: delivery acknowledgement from the app's push handler. Ids of other
    customers, unknown ids and rows before the history horizon are ignored silently (no enumeration)."""
    instant = timeutils.now() if at is None else at
    wanted = sorted({int(pk) for pk in inbox_ids if int(pk) > 0})  # ids ≤ 0 are unknown ids
    owned = list(
        selectors.inbox_queryset(customer_id, horizon).filter(pk__in=wanted).values_list("pk", flat=True)
    )
    return AckResult(acknowledged=len(owned), newly=_acknowledge(owned, instant))


@dataclass(frozen=True, slots=True)
class ReadResult:
    id: int
    read_at: Any
    unread_count: int


@transaction.atomic
def mark_read(customer_id: int, inbox_id: int, *, horizon: Any = None, at: Any = None) -> ReadResult:
    """``POST me/notifications/{id}/read/`` (idempotent; 404 for another customer's row). Reading implies
    delivery: the row goes through the same acknowledgement as ``ack/`` before ``read_at`` is set."""
    instant = timeutils.now() if at is None else at
    if not selectors.inbox_queryset(customer_id, horizon).filter(pk=inbox_id).exists():
        raise NotFound
    _acknowledge([inbox_id], instant)
    InboxNotification.objects.filter(pk=inbox_id, read_at__isnull=True).update(
        read_at=instant, updated_at=instant
    )
    read_at = InboxNotification.objects.filter(pk=inbox_id).values_list("read_at", flat=True).get()
    return ReadResult(id=inbox_id, read_at=read_at, unread_count=selectors.unread_count(customer_id, horizon))


@dataclass(frozen=True, slots=True)
class ReadAllResult:
    updated: int
    unread_count: int


@transaction.atomic
def mark_all_read(customer_id: int, *, horizon: Any = None, at: Any = None) -> ReadAllResult:
    """``POST me/notifications/read-all/``: every unread row (inside the horizon) is acknowledged, then read —
    one set-based conditional update per table."""
    instant = timeutils.now() if at is None else at
    unread = list(
        selectors.inbox_queryset(customer_id, horizon)
        .filter(read_at__isnull=True)
        .values_list("pk", flat=True)
    )
    _acknowledge(unread, instant)
    updated = InboxNotification.objects.filter(pk__in=unread, read_at__isnull=True).update(
        read_at=instant, updated_at=instant
    )
    return ReadAllResult(updated=updated, unread_count=selectors.unread_count(customer_id, horizon))


__all__ = [
    "AckResult",
    "ReadAllResult",
    "ReadResult",
    "acknowledge",
    "mark_all_read",
    "mark_read",
]
