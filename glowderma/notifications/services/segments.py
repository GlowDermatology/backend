"""Customer segments and their manual members."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from django.db import transaction
from rest_framework.exceptions import NotFound

from glowderma.core import audit
from glowderma.core.api.exceptions import Conflict
from glowderma.notifications import selectors
from glowderma.notifications.models import (
    Campaign,
    CustomerSegment,
    SegmentMember,
)
from glowderma.notifications.services import push_messages


def _segment_image(segment: CustomerSegment) -> dict[str, Any]:
    return {
        "name": segment.name,
        "min_online_orders": segment.min_online_orders,
        "min_lifetime_amount": segment.min_lifetime_amount,
    }


@transaction.atomic
def create_segment(
    *,
    name: str,
    min_online_orders: int | None,
    min_lifetime_amount: int | None,
    actor: Any,
    request: Any = None,
) -> CustomerSegment:
    segment = CustomerSegment.objects.create(
        name=name,
        min_online_orders=min_online_orders,
        min_lifetime_amount=min_lifetime_amount,
        created_by=actor,
    )
    audit.record(
        action="segment.created",
        entity_type="segment",
        entity_id=str(segment.pk),
        actor=actor,
        request=request,
        after=_segment_image(segment),
    )
    return segment


def _ensure_segment_free(segment: CustomerSegment) -> None:
    """409 ``conflict`` while an approved ``scheduled`` campaign targets the segment: its recipients are
    materialised at dispatch, so the audience the superuser approved must not grow meanwhile (through a rule
    change or new members — contract §2.8; the delete is refused for the same reason). Narrowing (removing
    a member) and renaming stay allowed; once the beat started the campaign the segment is free again."""
    if Campaign.objects.filter(audience_segment=segment, state=Campaign.State.SCHEDULED).exists():
        raise Conflict(push_messages.MESSAGES["segment_in_use"])


@transaction.atomic
def update_segment(
    segment: CustomerSegment, changes: dict[str, Any], *, actor: Any, request: Any = None
) -> CustomerSegment:
    """``PATCH segments/{id}/``: a rule change while a scheduled campaign targets the segment → 409."""
    segment = CustomerSegment.objects.select_for_update().get(pk=segment.pk)
    before = _segment_image(segment)
    if any(before.get(key) != value for key, value in changes.items() if key != "name"):
        _ensure_segment_free(segment)
    for key, value in changes.items():
        setattr(segment, key, value)
    segment.save()
    after = _segment_image(segment)
    if after != before:
        audit.record(
            action="segment.updated",
            entity_type="segment",
            entity_id=str(segment.pk),
            actor=actor,
            request=request,
            before={key: value for key, value in before.items() if after[key] != value},
            after={key: value for key, value in after.items() if before[key] != value},
        )
    return segment


@transaction.atomic
def delete_segment(segment: CustomerSegment, *, actor: Any, request: Any = None) -> None:
    """409 ``conflict`` while a scheduled campaign targets the segment (contract §2.8)."""
    segment = CustomerSegment.objects.select_for_update().get(pk=segment.pk)
    _ensure_segment_free(segment)
    before = _segment_image(segment)
    pk = segment.pk
    segment.delete()
    audit.record(
        action="segment.deleted",
        entity_type="segment",
        entity_id=str(pk),
        actor=actor,
        request=request,
        before=before,
    )


@transaction.atomic
def add_members(
    segment: CustomerSegment, customer_ids: Sequence[int], *, actor: Any, request: Any = None
) -> CustomerSegment:
    """``POST segments/{id}/members/``: ``bulk_create(ignore_conflicts=True)``; a known member is a no-op;
    409 ``conflict`` while a scheduled campaign targets the segment (the audience must not grow)."""
    segment = CustomerSegment.objects.select_for_update().get(pk=segment.pk)
    _ensure_segment_free(segment)
    existing = set(
        SegmentMember.objects.filter(segment=segment, customer_id__in=customer_ids).values_list(
            "customer_id", flat=True
        )
    )
    added = sorted({int(pk) for pk in customer_ids} - existing)
    SegmentMember.objects.bulk_create(
        [SegmentMember(segment=segment, customer_id=pk, added_by=actor) for pk in added],
        ignore_conflicts=True,
    )
    audit.record(
        action="segment.members_added",
        entity_type="segment",
        entity_id=str(segment.pk),
        actor=actor,
        request=request,
        after={"customer_ids": [int(pk) for pk in customer_ids], "added": added},
    )
    return segment


@transaction.atomic
def remove_member(segment: CustomerSegment, customer_id: int, *, actor: Any, request: Any = None) -> None:
    """``DELETE segments/{id}/members/{customer_id}/``: manual membership only; a rule-only member → 409."""
    membership = SegmentMember.objects.filter(segment=segment, customer_id=customer_id).first()
    if membership is None:
        if selectors.matches_rules(segment, customer_id):
            raise Conflict(push_messages.MESSAGES["rule_member"])
        raise NotFound
    membership.delete()
    audit.record(
        action="segment.member_removed",
        entity_type="segment",
        entity_id=str(segment.pk),
        actor=actor,
        request=request,
        after={"customer_id": customer_id},
    )


__all__ = [
    "add_members",
    "create_segment",
    "delete_segment",
    "remove_member",
    "update_segment",
]
