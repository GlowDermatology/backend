"""A campaign's lifecycle after its draft: approval, return to draft, cancel, stop and the start of due
campaigns (their recipients materialised)."""

from __future__ import annotations

from typing import Any

from django.db import transaction

from glowderma.core import enums, outbox, timeutils
from glowderma.core.api.exceptions import Conflict, InvalidTransition
from glowderma.notifications import selectors, state, topics
from glowderma.notifications.models import (
    Campaign,
    CampaignRecipient,
)
from glowderma.notifications.services.campaigns import common


def _finish_empty(campaign: Campaign, now: Any) -> None:
    """A campaign without a single pushable recipient goes straight to ``sent`` with zero counters."""
    campaign.state = Campaign.State.SENT
    campaign.started_at = campaign.started_at or now
    campaign.sent_at = now
    campaign.sent = campaign.failed = 0


def materialise(campaign: Campaign) -> int:
    """Insert one ``CampaignRecipient`` per pushable device of the audience (idempotent) and recompute
    ``recipient_count``; returns the count."""
    recipients = selectors.audience_recipients(campaign)
    CampaignRecipient.objects.bulk_create(
        [
            CampaignRecipient(campaign=campaign, customer_id=customer_id, device_id=device_id)
            for customer_id, device_id in recipients
        ],
        ignore_conflicts=True,
    )
    campaign.recipient_count = CampaignRecipient.objects.filter(campaign=campaign).count()
    return campaign.recipient_count


def start_campaign(campaign: Campaign) -> None:
    """``scheduled`` → ``sending`` (recipients materialised, live tallies at 0, batch 0 enqueued) or ``sent``
    at once when nobody can be reached; audited ``notification.send_started`` by the system. The caller
    (``start_due_campaigns``) saves the row inside the same transaction."""
    now = timeutils.now()
    before = {"state": campaign.state, "dispatch_after": common.iso(campaign.dispatch_after)}
    if materialise(campaign) == 0:
        _finish_empty(campaign, now)
    else:
        campaign.state = Campaign.State.SENDING
        campaign.started_at = now
        campaign.sent = campaign.failed = 0
        outbox.enqueue(
            topics.CAMPAIGN_SEND_BATCH,
            {"campaign_id": campaign.pk, "batch_no": 0},
            dedupe_key=topics.campaign_send_batch_key(campaign.pk, 0),
        )
    common.audit_campaign(
        campaign,
        state.transition("start").audit_action,
        actor=None,
        request=None,
        before=before,
        after={
            "state": campaign.state,
            "started_at": timeutils.isoformat(now),
            "recipient_count": campaign.recipient_count,
        },
    )


@transaction.atomic
def approve_campaign(
    campaign_id: int, *, expected_revision: int, actor: Any, request: Any = None
) -> Campaign:
    """``POST notifications/{id}/approve/`` (superuser): a ``tested`` campaign whose confirmed test is the
    current revision becomes ``scheduled`` with ``dispatch_after = max(now + approval delay, scheduled_at)``.
    **Nothing is dispatched here** — no outbox row, no on-commit hook, no FCM call: the beat
    (``start_due_campaigns``) claims it once ``dispatch_after`` has passed. ``expected_revision`` stale → 409
    ``conflict``; a ``draft`` → 409 ``invalid_transition`` :data:`MESSAGE_NOT_TESTED`, any other state
    :data:`state.MESSAGE_NOT_APPROVABLE`; a repeat on the approved revision answers unchanged. Audit
    ``notification.approved``."""
    campaign = common.lock_campaign(campaign_id)
    if expected_revision != campaign.content_revision:
        raise Conflict(common.MESSAGE_STALE_REVISION)
    if campaign.state == Campaign.State.DRAFT:  # the one wrong state with a remedy: test and confirm first
        raise InvalidTransition(common.MESSAGE_NOT_TESTED)
    if not state.check("approve", campaign.state):
        if campaign.approved_revision == campaign.content_revision:
            return campaign
        raise Conflict(
            common.MESSAGE_STALE_REVISION
        )  # approved for another revision: never happens, never ignored
    if campaign.tested_revision != campaign.content_revision:
        raise Conflict(common.MESSAGE_NOT_TESTED)
    if campaign.audience_type == enums.AudienceType.SEGMENT and campaign.audience_segment_id is None:
        raise Conflict(common.MESSAGE_SEGMENT_GONE)
    now = timeutils.now()
    earliest = now + common.approval_delay()
    dispatch_after = max(earliest, campaign.scheduled_at) if campaign.scheduled_at else earliest
    before = {"state": campaign.state}
    campaign.state = Campaign.State.SCHEDULED
    campaign.approved_at, campaign.approved_by = now, actor
    campaign.approved_revision = campaign.content_revision
    campaign.dispatch_after = dispatch_after
    campaign.recipient_count = len(selectors.audience_recipients(campaign))
    campaign.save(
        update_fields=[
            "state",
            "approved_at",
            "approved_by",
            "approved_revision",
            "dispatch_after",
            "recipient_count",
        ]
    )
    common.audit_campaign(
        campaign,
        state.transition("approve").audit_action,
        actor=actor,
        request=request,
        before=before,
        after={
            "state": campaign.state,
            "approved_revision": campaign.approved_revision,
            "dispatch_after": timeutils.isoformat(dispatch_after),
            "scheduled_at": timeutils.isoformat(campaign.scheduled_at) if campaign.scheduled_at else None,
            "recipient_count": campaign.recipient_count,
        },
    )
    return campaign


@transaction.atomic
def return_to_draft(campaign_id: int, *, actor: Any, request: Any = None) -> Campaign:
    """``POST notifications/{id}/return-to-draft/`` (superuser): a ``scheduled`` (not yet claimed) or
    ``cancelled`` (never started) campaign goes back to ``draft`` — approval, cancellation and test
    confirmation cleared, revision bumped so it must be tested again. Audit
    ``notification.returned_to_draft``."""
    campaign = common.lock_campaign(campaign_id)
    if not state.check("return_to_draft", campaign.state):
        return campaign
    if campaign.started_at is not None:  # a cancelled-by-stop campaign: pushes went out, nothing to redo
        raise InvalidTransition(state.MESSAGE_NOT_RETURNABLE)
    before = {"state": campaign.state, "revision": campaign.content_revision}
    campaign.state = Campaign.State.DRAFT
    campaign.content_revision += 1
    campaign.approved_at = campaign.approved_by = campaign.approved_revision = None
    campaign.dispatch_after = None
    campaign.cancelled_at = campaign.cancelled_by = None
    common.clear_test_confirmation(campaign)
    campaign.save()
    common.audit_campaign(
        campaign,
        state.transition("return_to_draft").audit_action,
        actor=actor,
        request=request,
        before=before,
        after={"state": campaign.state, "revision": campaign.content_revision},
    )
    return campaign


def cancel_campaign(campaign_id: int, *, actor: Any, request: Any = None) -> Campaign:
    """``POST notifications/{id}/cancel/`` (superuser; admin contract §2.8): an approved ``scheduled``
    campaign the beat has not claimed becomes ``cancelled`` (``cancelled_at`` / ``cancelled_by``; nothing was
    materialised or sent), audited ``notification.cancelled``. Idempotent on ``cancelled``. A ``draft`` /
    ``tested`` campaign → 409 ``invalid_transition`` :data:`MESSAGE_CANCEL_DRAFT`; once the send started →
    409 ``conflict`` :data:`MESSAGE_CANCEL_STARTED` (the emergency stop is ``stop/``). The row lock orders it
    against ``start_due_campaigns`` (which skips a locked row and re-reads the state)."""
    with transaction.atomic():
        campaign = common.lock_campaign(campaign_id)
        if campaign.state in Campaign.EDITABLE_STATES:
            raise InvalidTransition(common.MESSAGE_CANCEL_DRAFT)
        if campaign.state == Campaign.State.CANCELLED:
            return campaign
        if campaign.state != Campaign.State.SCHEDULED:
            raise Conflict(common.MESSAGE_CANCEL_STARTED)
        before = {
            "state": campaign.state,
            "scheduled_at": common.iso(campaign.scheduled_at),
            "dispatch_after": common.iso(campaign.dispatch_after),
        }
        cancelled_at = timeutils.now()
        campaign.state = Campaign.State.CANCELLED
        campaign.cancelled_at, campaign.cancelled_by = cancelled_at, actor
        campaign.save(update_fields=["state", "cancelled_at", "cancelled_by"])
        common.audit_campaign(
            campaign,
            state.transition("cancel").audit_action,
            actor=actor,
            request=request,
            before=before,
            after={"state": campaign.state, "cancelled_at": timeutils.isoformat(cancelled_at)},
        )
        return campaign


@transaction.atomic
def request_stop(campaign_id: int, *, actor: Any, request: Any = None) -> Campaign:
    """``POST notifications/{id}/stop/`` (superuser; emergency stop): ``sending`` → ``stopping``. The running
    batch checks the state before claiming and before enqueueing the next batch and finalises the campaign
    (``partially_sent`` / ``cancelled``, pending recipients ``stopped``); pushes FCM already accepted are
    never recalled. Idempotent on ``stopping`` and on a campaign that ended by a stop; anything else → 409
    ``invalid_transition`` :data:`MESSAGE_NOT_SENDING`. Audit ``notification.stop_requested``."""
    campaign = common.lock_campaign(campaign_id)
    if campaign.stop_requested_at is not None:
        return campaign
    state.check("stop", campaign.state)
    now = timeutils.now()
    campaign.state = Campaign.State.STOPPING
    campaign.stop_requested_at, campaign.stop_requested_by = now, actor
    campaign.save(update_fields=["state", "stop_requested_at", "stop_requested_by"])
    common.audit_campaign(
        campaign,
        state.transition("stop").audit_action,
        actor=actor,
        request=request,
        before={"state": Campaign.State.SENDING, "sent": campaign.sent, "failed": campaign.failed},
        after={"state": campaign.state, "stop_requested_at": timeutils.isoformat(now)},
    )
    return campaign


def start_due_campaigns(now: Any = None) -> list[int]:
    """Beat ``notifications.start_due_campaigns`` (1 min): start every approved ``scheduled`` campaign whose
    ``dispatch_after`` has passed, one transaction each (``select_for_update(skip_locked=True)`` keeps two
    beats apart; the state is re-read under the lock, so a cancel that won the lock is honoured)."""
    instant = timeutils.now() if now is None else now
    started: list[int] = []
    due = list(
        Campaign.objects.filter(
            state=Campaign.State.SCHEDULED, approved_at__isnull=False, dispatch_after__lte=instant
        )
        .order_by("dispatch_after", "id")
        .values_list("pk", flat=True)
    )
    for pk in due:
        with transaction.atomic():
            campaign = (
                Campaign.objects.select_for_update(skip_locked=True)
                .filter(
                    pk=pk,
                    state=Campaign.State.SCHEDULED,
                    approved_at__isnull=False,
                    dispatch_after__lte=instant,
                )
                .first()
            )
            if campaign is None:
                continue
            start_campaign(campaign)
            campaign.save()
            started.append(pk)
    return started


__all__ = [
    "approve_campaign",
    "cancel_campaign",
    "materialise",
    "request_stop",
    "return_to_draft",
    "start_campaign",
    "start_due_campaigns",
]
