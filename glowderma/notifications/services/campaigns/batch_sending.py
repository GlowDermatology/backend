"""The batched send of a campaign: claim a batch of recipients, push it, settle every claim, and finalise
stopped or failed campaigns."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from django.db import transaction
from django.db.models import Count, Q

from glowderma.core import outbox, timeutils
from glowderma.core.models import OutboxMessage
from glowderma.integrations.fcm.ports import (
    NOT_SENT,
    FcmUnavailable,
    PushMessage,
    PushResult,
)
from glowderma.integrations.fcm.registry import get_fcm_port
from glowderma.notifications import selectors, state, topics
from glowderma.notifications.models import (
    Campaign,
    CampaignRecipient,
    InboxNotification,
)
from glowderma.notifications.services import push_messages
from glowderma.notifications.services.campaigns import common


@dataclass(slots=True)
class _Claim:
    recipient: CampaignRecipient
    inbox: InboxNotification
    token: str


def _claim_batch(campaign: Campaign, recipients: Sequence[CampaignRecipient]) -> list[_Claim]:
    """Create the inbox rows and claim the deliveries (``push_state = sending``) of the pending recipients."""
    claims: list[_Claim] = []
    for recipient in recipients:
        if recipient.state != CampaignRecipient.State.PENDING:
            continue  # a redelivered batch: already settled
        token = push_messages.usable_token(recipient.device)
        inbox, _created = InboxNotification.objects.get_or_create(
            dedupe_key=topics.campaign_inbox_key(campaign.pk, recipient.customer_id),
            defaults={
                "customer_id": recipient.customer_id,
                **push_messages.inbox_defaults(
                    campaign,
                    InboxNotification.PushState.QUEUED if token else InboxNotification.PushState.NO_DEVICE,
                ),
            },
        )
        recipient.inbox = inbox
        if not token:
            recipient.state = CampaignRecipient.State.SKIPPED
            recipient.error = push_messages.NO_DEVICE
        elif inbox.push_state == InboxNotification.PushState.QUEUED:
            inbox.push_state = InboxNotification.PushState.SENDING
            inbox.save(update_fields=["push_state"])
            claims.append(_Claim(recipient, inbox, token))
        elif inbox.push_state == InboxNotification.PushState.SENT:
            recipient.state = CampaignRecipient.State.SENT
            recipient.sent_at = inbox.pushed_at or timeutils.now()
        else:  # sending (a crash after the claim), failed, no_device: never resent
            recipient.state = CampaignRecipient.State.FAILED
            recipient.error = (
                push_messages.INTERRUPTED
                if inbox.push_state == InboxNotification.PushState.SENDING
                else inbox.push_error
            ) or inbox.push_state
        recipient.save(update_fields=["inbox", "state", "error", "sent_at"])
    return claims


def _push_messages(campaign: Campaign, claims: Iterable[_Claim]) -> list[PushMessage]:
    return common.campaign_messages(campaign, [(claim.token, claim.inbox.pk) for claim in claims])


def _release(claims: Iterable[_Claim]) -> None:
    """The provider was unreachable before anything was accepted: give the claims back for the retry."""
    with transaction.atomic():
        for claim in claims:
            InboxNotification.objects.filter(
                pk=claim.inbox.pk, push_state=InboxNotification.PushState.SENDING
            ).update(push_state=InboxNotification.PushState.QUEUED)


def _settle(claim: _Claim, result: PushResult, now: Any) -> None:
    recipient, inbox = claim.recipient, claim.inbox
    if result.ok:
        recipient.state = CampaignRecipient.State.SENT
        recipient.sent_at = now
        recipient.provider_message_id = result.message_id
        inbox.push_state = InboxNotification.PushState.SENT
        inbox.pushed_at = now
    else:
        recipient.state = CampaignRecipient.State.FAILED
        recipient.error = result.error
        recipient.provider_state = {"error": result.error}
        inbox.push_state = InboxNotification.PushState.FAILED
        inbox.push_error = result.error
        push_messages.after_failure(
            result, claim.token, recipient.device_id, topics.CAMPAIGN_SEND_BATCH, inbox.pk
        )
    recipient.save(update_fields=["state", "sent_at", "error", "provider_message_id", "provider_state"])
    # a filtered update: a finalisation that overlapped this delivery may have withdrawn the row already
    InboxNotification.objects.filter(pk=inbox.pk).update(
        push_state=inbox.push_state, pushed_at=inbox.pushed_at, push_error=inbox.push_error
    )


def _stop_claim(claim: _Claim) -> None:
    """A claim FCM did not take while the campaign is stopping: never attempted again (recipient
    ``stopped``, its in-app row withdrawn) — the claim is not given back."""
    recipient = claim.recipient
    recipient.state, recipient.error = CampaignRecipient.State.STOPPED, common.STOPPED
    recipient.save(update_fields=["state", "error"])
    withdraw_inbox([claim.inbox.pk])


def withdraw_inbox(inbox_ids: Sequence[int]) -> None:
    """Delete the in-app rows of recipients a stop or a dead batch leaves ``stopped`` before any push went
    out (``queued``: given back by FCM, ``sending``: claimed when the worker died). The customer never got
    the push, so the withdrawn campaign must not sit in the app's feed either (``inbox_queryset`` shows every
    row); ``CampaignRecipient.inbox`` is ``SET_NULL``. A row FCM accepted (``sent``) is never touched."""
    if inbox_ids:
        InboxNotification.objects.filter(
            pk__in=inbox_ids,
            push_state__in=[InboxNotification.PushState.QUEUED, InboxNotification.PushState.SENDING],
        ).delete()


def _stop_pending(campaign: Campaign) -> None:
    """Pending recipients become ``stopped`` and their never-pushed in-app rows are withdrawn."""
    pending = CampaignRecipient.objects.filter(campaign=campaign, state=CampaignRecipient.State.PENDING)
    inbox_ids = list(pending.exclude(inbox__isnull=True).values_list("inbox_id", flat=True))
    pending.update(state=CampaignRecipient.State.STOPPED, error=common.STOPPED)
    withdraw_inbox(inbox_ids)


def _refresh_tally(campaign: Campaign) -> None:
    """``sent`` / ``failed`` from the recipient states in one aggregate (``skipped`` counts as failed;
    ``pending`` and ``stopped`` count in neither, so ``attempted = sent + failed`` holds at every moment)."""
    tally = CampaignRecipient.objects.filter(campaign=campaign).aggregate(
        sent=Count("id", filter=Q(state=CampaignRecipient.State.SENT)),
        failed=Count(
            "id",
            filter=Q(state__in=[CampaignRecipient.State.FAILED, CampaignRecipient.State.SKIPPED]),
        ),
    )
    campaign.sent, campaign.failed = int(tally["sent"] or 0), int(tally["failed"] or 0)


def _finalise_stopped(campaign: Campaign) -> None:
    """``stopping`` → ``partially_sent`` when at least one push went out, else ``cancelled`` (by the staff
    member who asked for the stop): pending recipients become ``stopped`` and their never-pushed in-app rows
    are withdrawn, the tallies are final, ``stopped_at`` is set. Audited ``notification.stopped``. The caller
    holds the row lock and saves."""
    now = timeutils.now()
    _stop_pending(campaign)
    _refresh_tally(campaign)
    campaign.stopped_at = now
    if campaign.sent:
        campaign.state = Campaign.State.PARTIALLY_SENT
    else:
        campaign.state = Campaign.State.CANCELLED
        campaign.cancelled_at = now
        campaign.cancelled_by = campaign.stop_requested_by
    common.audit_campaign(
        campaign,
        state.transition("stopped").audit_action,
        actor=None,
        request=None,
        before={"state": Campaign.State.STOPPING},
        after={
            "state": campaign.state,
            "sent": campaign.sent,
            "failed": campaign.failed,
            "stopped_at": timeutils.isoformat(now),
        },
    )


def finalise_stopping_campaigns() -> list[int]:
    """Beat ``notifications.finalise_stopping_campaigns`` (1 min): a ``stopping`` campaign with no open
    ``campaign.send_batch`` message left (the worker died, or the stop came after the last batch) is
    finalised here, so it never stays "To‘xtatilmoqda" for ever."""
    finalised: list[int] = []
    for pk in list(
        Campaign.objects.filter(state=Campaign.State.STOPPING).order_by("id").values_list("pk", flat=True)
    ):
        with transaction.atomic():
            campaign = (
                Campaign.objects.select_for_update(skip_locked=True)
                .filter(pk=pk, state=Campaign.State.STOPPING)
                .first()
            )
            if campaign is None or selectors.has_open_batch(pk):
                continue
            _finalise_stopped(campaign)
            campaign.save()
            finalised.append(pk)
    return finalised


def fail_campaign_from_dead_batch(message: OutboxMessage) -> None:
    """``on_push_dead`` of a ``campaign.send_batch`` message (open question M3-N1, decided): the campaign
    ends ``partially_sent`` when a push went out, else ``failed`` — ``failed_at`` now, ``last_error`` the
    message's, pending recipients ``stopped`` (their never-pushed in-app rows withdrawn). Audited
    ``notification.failed``. Idempotent: a campaign no longer sending is left alone."""
    payload: dict[str, Any] = message.payload if isinstance(message.payload, dict) else {}
    campaign_id = payload.get("campaign_id")
    if campaign_id is None:
        return
    with transaction.atomic():
        campaign = Campaign.objects.select_for_update().filter(pk=int(campaign_id)).first()
        if campaign is None or campaign.state not in state.transition("failed").sources:
            return
        now = timeutils.now()
        before = {"state": campaign.state}
        _stop_pending(campaign)
        _refresh_tally(campaign)
        campaign.failed_at = now
        campaign.last_error = (message.last_error or "")[:2000]
        campaign.state = Campaign.State.PARTIALLY_SENT if campaign.sent else Campaign.State.FAILED
        campaign.save()
        common.audit_campaign(
            campaign,
            state.transition("failed").audit_action,
            actor=None,
            request=None,
            before=before,
            after={
                "state": campaign.state,
                "sent": campaign.sent,
                "failed": campaign.failed,
                "outbox_message_id": message.pk,
                "last_error": campaign.last_error,
            },
        )


def send_batch(campaign_id: int, batch_no: int) -> None:
    """Handler body of ``campaign.send_batch`` (design 03 §3.4): claim → provider → settle → cursor → next.

    A redelivered batch (``batch_no < batches_dispatched``) or a finished campaign is a no-op; an
    out-of-order one raises so the dispatcher retries it after the previous batch committed.

    Emergency stop: a ``stopping`` campaign is finalised **before claiming** (transaction 1: nothing more
    goes out) and **before enqueueing the next batch** (transaction 2: the results of the pushes already in
    flight are settled, claims FCM did not take become ``stopped``, no next batch, no deferral).

    Single-flight under an overlapping redelivery (the outbox lease expired while this worker was inside the
    provider call and another worker re-ran the same batch): the claims are exclusive (``push_state`` queued →
    sending under the campaign lock), and the advance is a compare-and-set under the lock — only the delivery
    that still sees ``batches_dispatched == batch_no`` moves the cursor, bumps the counter and enqueues the
    next batch or finishes. A delivery that lost the race settles only its own claims, refreshes the final
    tally when the campaign already finished, and enqueues nothing.

    Pushes FCM did not take (``not_sent``: a 429/5xx answer, a batch that stopped or ran out of time) are
    given back (``queued``, recipient still ``pending``) while the others are settled; the cursor stays and
    ``outbox.RetryLater`` delivers this same batch again after :data:`NOT_SENT_RETRY`, which claims only the
    given-back recipients (no attempt spent; dead after :data:`NOT_SENT_GIVE_UP`). The live tallies
    (``sent`` / ``failed``) are refreshed after every batch.
    """
    with transaction.atomic():
        campaign = Campaign.objects.select_for_update().get(pk=campaign_id)
        if campaign.state in Campaign.TERMINAL_STATES or batch_no < campaign.batches_dispatched:
            return
        if campaign.state == Campaign.State.STOPPING:
            _finalise_stopped(campaign)
            campaign.save()
            return
        if campaign.state != Campaign.State.SENDING or batch_no > campaign.batches_dispatched:
            raise RuntimeError(f"campaign {campaign_id} is not ready for batch {batch_no}")
        recipients = list(
            CampaignRecipient.objects.filter(campaign=campaign, id__gt=campaign.send_cursor)
            .select_related("device")
            .order_by("id")[: push_messages.batch_size()]
        )
        claims = _claim_batch(campaign, recipients)
    try:
        results = get_fcm_port().send(_push_messages(campaign, claims)) if claims else []
    except FcmUnavailable:
        _release(claims)
        raise
    not_sent = [claim for claim, result in zip(claims, results, strict=True) if result.error == NOT_SENT]
    with transaction.atomic():
        campaign = Campaign.objects.select_for_update().get(pk=campaign_id)
        now = timeutils.now()
        stopping = campaign.state == Campaign.State.STOPPING
        lost = (
            campaign.state not in (Campaign.State.SENDING, Campaign.State.STOPPING)
            or campaign.batches_dispatched != batch_no
        )
        for claim, result in zip(claims, results, strict=True):
            if result.error != NOT_SENT or lost:
                _settle(claim, result, now)
            elif stopping:
                _stop_claim(claim)
        if lost:
            # Another delivery of this batch already advanced it (compare-and-set lost).
            if campaign.state in Campaign.TERMINAL_STATES:
                _refresh_tally(campaign)
                campaign.save(update_fields=["sent", "failed"])
            return
        if stopping:
            if recipients:
                campaign.send_cursor = recipients[-1].pk
            campaign.batches_dispatched += 1
            _finalise_stopped(campaign)
            campaign.save()
            return
        if not_sent:
            # FCM did not take these: give them back and deliver this batch again later (the cursor stays)
            _release(not_sent)
            _refresh_tally(campaign)
        else:
            if recipients:
                campaign.send_cursor = recipients[-1].pk
            campaign.batches_dispatched += 1
            remaining = CampaignRecipient.objects.filter(
                campaign=campaign, id__gt=campaign.send_cursor
            ).exists()
            _refresh_tally(campaign)
            if remaining:
                outbox.enqueue(
                    topics.CAMPAIGN_SEND_BATCH,
                    {"campaign_id": campaign.pk, "batch_no": batch_no + 1},
                    dedupe_key=topics.campaign_send_batch_key(campaign.pk, batch_no + 1),
                )
            else:
                campaign.state = Campaign.State.SENT
                campaign.sent_at = now
        campaign.save()
    if not_sent:
        raise outbox.RetryLater(
            push_messages.NOT_SENT_RETRY,
            f"campaign {campaign_id} batch {batch_no}: {len(not_sent)} push(es) not sent by FCM",
            give_up_after=push_messages.NOT_SENT_GIVE_UP,
        )


__all__ = [
    "fail_campaign_from_dead_batch",
    "finalise_stopping_campaigns",
    "send_batch",
    "withdraw_inbox",
]
