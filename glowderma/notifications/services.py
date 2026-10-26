"""Writes of `notifications` (design 03 §3): campaigns and their batched send, status-push deliveries,
templates, segments and manual members.

Delivery is at-least-once: every handler body here is idempotent at the provider boundary through the
``InboxNotification.dedupe_key`` claim protocol (§3.6) and the ``CampaignRecipient`` states (§3.5).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import F, Value
from django.db.models.functions import Coalesce
from rest_framework.exceptions import NotFound, ValidationError

from glowderma.accounts import services as account_services
from glowderma.accounts import topics as account_topics
from glowderma.accounts.models import Customer, Device
from glowderma.appointments import selectors as appointment_selectors
from glowderma.appointments.models import OPEN_STATUSES
from glowderma.catalog.models import Product
from glowderma.core import audit, enums, i18n, outbox, timeutils
from glowderma.core.api.exceptions import Conflict, field_error
from glowderma.core.images import ImageError, process_image
from glowderma.core.media import absolute_media_url, media_name
from glowderma.integrations.fcm.ports import (
    NOT_SENT,
    PAYLOAD_INVALID,
    UNREGISTERED,
    FcmUnavailable,
    PushMessage,
    PushResult,
)
from glowderma.integrations.fcm.registry import get_fcm_port
from glowderma.notifications import selectors, texts, topics
from glowderma.notifications.media import CAMPAIGN_IMAGE_KIND
from glowderma.notifications.models import (
    Campaign,
    CampaignRecipient,
    CustomerSegment,
    InboxNotification,
    SegmentMember,
    StatusTemplate,
)

logger = logging.getLogger(__name__)

MESSAGES = {
    "segment_in_use": "Rejalashtirilgan xabar bu guruhga yuboriladi.",
    "rule_member": "Mijoz guruhga qoida orqali kiradi.",
    "unknown_placeholder": "Faqat {raqam} o‘rinbosari ishlatiladi.",
}
IMAGE_MESSAGES = {"file_too_large": None, "unsupported_image": None}
INTERRUPTED = "interrupted"  # a claim left in ``sending`` by a crash: never resent (design 03 §3.6)
NO_DEVICE = "no_device"
# M3 live FCM: messages FCM did not take (``not_sent``: 429/5xx answers, the batch stopped or ran out of time)
# are given back and the same outbox message is delivered again after NOT_SENT_RETRY, without spending its
# attempts, for at most NOT_SENT_GIVE_UP; then it dies like any push (``notification.push_dead``).
NOT_SENT_RETRY = timedelta(minutes=1)
NOT_SENT_GIVE_UP = timedelta(hours=6)

# Customer contract §2.14.2: Android channel and priority per ``data.type`` (APNs: alert, priority 10 for
# "high", 5 for "normal").
PUSH_PROFILES: dict[str, tuple[str, str]] = {
    enums.PushDataType.ORDER_STATUS: ("orders", "high"),
    enums.PushDataType.APPOINTMENT: ("appointments", "high"),
    enums.PushDataType.ACCOUNT_SECURITY: ("general", "high"),
}
CAMPAIGN_PRIORITY = "normal"
PROVIDER_BRANDS = {"google": "Google", "facebook": "Facebook", "apple": "Apple"}


def push_message(
    token: str,
    title: str,
    body: str,
    data: dict[str, str],
    *,
    channel: str,
    priority: str,
    image_url: str = "",
) -> PushMessage:
    """A visible push with the channel/priority of its ``data.type`` (all ``data`` values are strings)."""
    return PushMessage(
        token,
        title,
        body,
        {key: str(value) for key, value in data.items()},
        android_channel=channel,
        android_priority=priority,
        apns_push_type="alert",
        apns_priority=10 if priority == "high" else 5,
        image_url=image_url,
    )


def _typed_message(token: str, title: str, body: str, data: dict[str, str]) -> PushMessage:
    channel, priority = PUSH_PROFILES[data["type"]]
    return push_message(token, title, body, data, channel=channel, priority=priority)


def _log_payload_invalid(topic: str, inbox_id: int | None) -> None:
    """A malformed message (C§2.14.2): alert and fail the message; the device is never touched."""
    logger.error("push.payload_invalid topic=%s field=message inbox_id=%s", topic, inbox_id)


def batch_size() -> int:
    return int(settings.CAMPAIGN_BATCH_SIZE)


# --- campaigns ----------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CampaignInput:
    kind: str
    title: str
    body: str
    link_kind: str = enums.LinkKind.NONE
    link_product: Product | None = None
    link_url: str = ""  # kind "url" only (gap ADM-10.1): a validated https link
    audience_type: str = enums.AudienceType.ALL
    audience_segment: CustomerSegment | None = None
    audience_customer: Customer | None = None
    scheduled_at: Any = None
    extra: dict[str, Any] = field(default_factory=dict)


def _store_image(campaign: Campaign, upload: Any) -> None:
    try:
        processed = process_image(upload)
    except ImageError as exc:
        raise ValidationError({"image": [field_error(exc.code)]}) from exc
    name = media_name(CAMPAIGN_IMAGE_KIND, campaign.pk, processed.content, processed.extension)
    campaign.image.save(name.rsplit("/", 1)[-1], ContentFile(processed.content), save=False)
    campaign.save(update_fields=["image"])


def _campaign_audit_image(campaign: Campaign) -> dict[str, Any]:
    return {
        "kind": campaign.kind,
        "title": campaign.title,
        "audience_type": campaign.audience_type,
        "audience_segment_id": campaign.audience_segment_id,
        "audience_phone": campaign.audience_phone or None,
        "link_kind": campaign.link_kind,
        "link_product_id": campaign.link_product_id,
        "link_url": campaign.link_url or None,
        "scheduled_at": timeutils.isoformat(campaign.scheduled_at) if campaign.scheduled_at else None,
        "state": campaign.state,
        "recipient_count": campaign.recipient_count,
    }


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


def _queue(campaign: Campaign) -> None:
    """``queued`` + batch 0 (or ``sent`` when nobody can be reached). The caller saves the row."""
    now = timeutils.now()
    if materialise(campaign) == 0:
        _finish_empty(campaign, now)
        return
    campaign.state = Campaign.State.QUEUED
    outbox.enqueue(
        topics.CAMPAIGN_SEND_BATCH,
        {"campaign_id": campaign.pk, "batch_no": 0},
        dedupe_key=topics.campaign_send_batch_key(campaign.pk, 0),
    )


def _label_snapshot(product: Product | None) -> str:
    """The uz display name at creation, cut to the column: ``billz_name`` (the fallback) holds up to 255."""
    if product is None:
        return ""
    limit = Campaign._meta.get_field("link_product_label").max_length or 120
    return product.display_name[:limit]


@transaction.atomic
def create_campaign(data: CampaignInput, image: Any = None, *, actor: Any, request: Any = None) -> Campaign:
    """``POST notifications/``: without ``scheduled_at`` the send starts now (recipients materialised, batch 0
    enqueued); with it the campaign waits for ``start_due_campaigns``. Audit ``notification.created``."""
    scheduled = data.scheduled_at is not None
    campaign = Campaign(
        kind=data.kind,
        title=data.title,
        body=data.body,
        link_kind=data.link_kind,
        link_product=data.link_product,
        link_product_label=_label_snapshot(data.link_product),
        link_url=data.link_url if data.link_kind == enums.LinkKind.URL else "",
        audience_type=data.audience_type,
        audience_segment=data.audience_segment,
        audience_segment_name=data.audience_segment.name if data.audience_segment is not None else "",
        audience_customer=data.audience_customer,
        audience_phone=data.audience_customer.phone if data.audience_customer is not None else "",
        state=Campaign.State.SCHEDULED if scheduled else Campaign.State.QUEUED,
        scheduled_at=data.scheduled_at,
        author=actor,
    )
    campaign.save()
    if image is not None:
        _store_image(campaign, image)
    if scheduled:
        campaign.recipient_count = len(selectors.audience_recipients(campaign))
    else:
        _queue(campaign)
    campaign.save()
    audit.record(
        action="notification.created",
        entity_type="campaign",
        entity_id=str(campaign.pk),
        actor=actor,
        request=request,
        after=_campaign_audit_image(campaign),
    )
    return campaign


MESSAGE_CANCEL_STARTED = "Yuborish boshlangan: xabarni bekor qilib bo‘lmaydi."


def cancel_campaign(campaign_id: int, *, actor: Any, request: Any = None) -> Campaign:
    """``POST notifications/{id}/cancel/`` (admin contract §2.8, M3): a ``scheduled`` campaign becomes
    ``cancelled`` (``cancelled_at`` = now; nothing was materialised or sent), audited
    ``notification.cancelled``.
    Idempotent: an already cancelled campaign is returned unchanged (no second audit row). Once the send
    started (``queued`` / ``sending`` / ``sent``) → 409 ``conflict`` :data:`MESSAGE_CANCEL_STARTED`. The row
    lock orders it against ``start_due_campaigns`` (which skips a locked row and re-reads the state)."""
    with transaction.atomic():
        campaign = Campaign.objects.select_for_update().filter(pk=campaign_id).first()
        if campaign is None:
            raise NotFound
        if campaign.state == Campaign.State.CANCELLED:
            return campaign
        if campaign.state != Campaign.State.SCHEDULED:
            raise Conflict(MESSAGE_CANCEL_STARTED)
        scheduled_at = campaign.scheduled_at
        before = {
            "state": campaign.state,
            "scheduled_at": timeutils.isoformat(scheduled_at) if scheduled_at else None,
        }
        cancelled_at = timeutils.now()
        campaign.state = Campaign.State.CANCELLED
        campaign.cancelled_at = cancelled_at
        campaign.save(update_fields=["state", "cancelled_at", "updated_at"])
        audit.record(
            action="notification.cancelled",
            entity_type="campaign",
            entity_id=str(campaign.pk),
            actor=actor,
            request=request,
            before=before,
            after={"state": campaign.state, "cancelled_at": timeutils.isoformat(cancelled_at)},
        )
        return campaign


def start_due_campaigns(now: Any = None) -> list[int]:
    """Beat ``notifications.start_due_campaigns`` (1 min): queue every ``scheduled`` campaign that is due,
    one transaction each (``select_for_update(skip_locked=True)`` keeps two beats apart)."""
    instant = timeutils.now() if now is None else now
    started: list[int] = []
    due = list(
        Campaign.objects.filter(state=Campaign.State.SCHEDULED, scheduled_at__lte=instant)
        .order_by("scheduled_at", "id")
        .values_list("pk", flat=True)
    )
    for pk in due:
        with transaction.atomic():
            campaign = (
                Campaign.objects.select_for_update(skip_locked=True)
                .filter(pk=pk, state=Campaign.State.SCHEDULED, scheduled_at__lte=instant)
                .first()
            )
            if campaign is None:
                continue
            _queue(campaign)
            campaign.save()
            started.append(pk)
    return started


@dataclass(slots=True)
class _Claim:
    recipient: CampaignRecipient
    inbox: InboxNotification
    token: str


def _inbox_defaults(campaign: Campaign, push_state: str) -> dict[str, Any]:
    link: dict[str, Any] = {"kind": "none"}
    if campaign.link_kind == enums.LinkKind.PRODUCT and campaign.link_product_id is not None:
        link = {"kind": "product", "product_id": campaign.link_product_id}
    elif campaign.link_kind == enums.LinkKind.URL and campaign.link_url:
        link = {"kind": enums.InboxLinkKind.URL.value, "url": campaign.link_url}  # gap ADM-10.1
    return {
        "kind": campaign.kind,
        "title": campaign.title,
        "body": campaign.body,
        "link": link,
        "image_url": absolute_media_url(campaign.image.name or "") if campaign.image else "",
        "source_type": InboxNotification.SourceType.CAMPAIGN,
        "source_id": campaign.pk,
        "language": enums.ContentLanguage.UZ,
        "push_state": push_state,
    }


def _usable_token(device: Device | None) -> str:
    if device is None or not device.is_active or not device.push_enabled or not device.fcm_token:
        return ""
    return device.fcm_token


def _claim_batch(campaign: Campaign, recipients: Sequence[CampaignRecipient]) -> list[_Claim]:
    """Create the inbox rows and claim the deliveries (``push_state = sending``) of the pending recipients."""
    claims: list[_Claim] = []
    for recipient in recipients:
        if recipient.state != CampaignRecipient.State.PENDING:
            continue  # a redelivered batch: already settled
        token = _usable_token(recipient.device)
        inbox, _created = InboxNotification.objects.get_or_create(
            dedupe_key=topics.campaign_inbox_key(campaign.pk, recipient.customer_id),
            defaults={
                "customer_id": recipient.customer_id,
                **_inbox_defaults(
                    campaign,
                    InboxNotification.PushState.QUEUED if token else InboxNotification.PushState.NO_DEVICE,
                ),
            },
        )
        recipient.inbox = inbox
        if not token:
            recipient.state = CampaignRecipient.State.SKIPPED
            recipient.error = NO_DEVICE
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
                INTERRUPTED if inbox.push_state == InboxNotification.PushState.SENDING else inbox.push_error
            ) or inbox.push_state
        recipient.save(update_fields=["inbox", "state", "error", "sent_at"])
    return claims


def _push_messages(campaign: Campaign, claims: Iterable[_Claim]) -> list[PushMessage]:
    """Campaign pushes (C§2.14.2): ``data`` ``{type, inbox_id, campaign_id, product_id | "", link_url | ""}``,
    channel ``promo`` / ``general``, normal priority, the campaign image as ``notification.image``.
    ``link_url`` is the campaign's https destination (kind ``url``, gap ADM-10.1)."""
    product_id = (
        str(campaign.link_product_id)
        if campaign.link_kind == enums.LinkKind.PRODUCT and campaign.link_product_id is not None
        else ""
    )
    link_url = campaign.link_url if campaign.link_kind == enums.LinkKind.URL else ""
    image_url = absolute_media_url(campaign.image.name or "") if campaign.image else ""
    channel = "promo" if campaign.kind == enums.NotificationKind.PROMO else "general"
    return [
        push_message(
            claim.token,
            campaign.title,
            campaign.body,
            {
                "type": enums.PushDataType.CAMPAIGN,
                "inbox_id": str(claim.inbox.pk),
                "campaign_id": str(campaign.pk),
                "product_id": product_id,
                "link_url": link_url,
            },
            channel=channel,
            priority=CAMPAIGN_PRIORITY,
            image_url=image_url,
        )
        for claim in claims
    ]


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
        _after_failure(result, claim.token, recipient.device_id, topics.CAMPAIGN_SEND_BATCH, inbox.pk)
    recipient.save(update_fields=["state", "sent_at", "error", "provider_message_id", "provider_state"])
    inbox.save(update_fields=["push_state", "pushed_at", "push_error"])


def _after_failure(result: PushResult, token: str, device_id: int | None, topic: str, inbox_id: int) -> None:
    """Token pruning only when FCM says the token is dead (``unregistered``); a malformed message
    (``payload_invalid``) is alerted and never touches a device (C§2.14.2)."""
    if result.error == UNREGISTERED and device_id is not None:
        account_services.invalidate_fcm_token(device_id, token=token)
    elif result.error == PAYLOAD_INVALID:
        _log_payload_invalid(topic, inbox_id)


def _refresh_tally(campaign: Campaign) -> None:
    """``sent`` / ``failed`` from the recipient states (``skipped`` counts as failed)."""
    states = CampaignRecipient.objects.filter(campaign=campaign).values_list("state", flat=True)
    tally = {"sent": 0, "failed": 0}
    for state in states:
        tally["sent" if state == CampaignRecipient.State.SENT else "failed"] += 1
    campaign.sent, campaign.failed = tally["sent"], tally["failed"]


def send_batch(campaign_id: int, batch_no: int) -> None:
    """Handler body of ``campaign.send_batch`` (design 03 §3.4): claim → provider → settle → cursor → next.

    A redelivered batch (``batch_no < batches_dispatched``) is a no-op; an out-of-order one raises so the
    dispatcher retries it after the previous batch committed.

    Single-flight under an overlapping redelivery (the outbox lease expired while this worker was inside the
    provider call and another worker re-ran the same batch): the claims are exclusive (``push_state`` queued →
    sending under the campaign lock), and the advance is a compare-and-set under the lock — only the delivery
    that still sees ``batches_dispatched == batch_no`` moves the cursor, bumps the counter and enqueues the
    next batch or finishes. A delivery that lost the race settles only its own claims, refreshes the final
    tally when the campaign already finished, and enqueues nothing.

    Pushes FCM did not take (``not_sent``: a 429/5xx answer, a batch that stopped or ran out of time) are
    given back (``queued``, recipient still ``pending``) while the others are settled; the cursor stays and
    ``outbox.RetryLater`` delivers this same batch again after :data:`NOT_SENT_RETRY`, which claims only the
    given-back recipients (no attempt spent; dead after :data:`NOT_SENT_GIVE_UP`).
    """
    with transaction.atomic():
        campaign = Campaign.objects.select_for_update().get(pk=campaign_id)
        if campaign.state == Campaign.State.SENT or batch_no < campaign.batches_dispatched:
            return
        if campaign.state == Campaign.State.SCHEDULED or batch_no > campaign.batches_dispatched:
            raise RuntimeError(f"campaign {campaign_id} is not ready for batch {batch_no}")
        if campaign.state == Campaign.State.QUEUED:
            campaign.state = Campaign.State.SENDING
            campaign.started_at = timeutils.now()
            campaign.save(update_fields=["state", "started_at"])
        recipients = list(
            CampaignRecipient.objects.filter(campaign=campaign, id__gt=campaign.send_cursor)
            .select_related("device")
            .order_by("id")[: batch_size()]
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
        lost = campaign.state != Campaign.State.SENDING or campaign.batches_dispatched != batch_no
        for claim, result in zip(claims, results, strict=True):
            if result.error != NOT_SENT or lost:
                _settle(claim, result, now)
        if lost:
            # Another delivery of this batch already advanced it (compare-and-set lost).
            if campaign.state == Campaign.State.SENT:
                _refresh_tally(campaign)
                campaign.save(update_fields=["sent", "failed"])
            return
        if not_sent:
            # FCM did not take these: give them back and deliver this batch again later (the cursor stays)
            _release(not_sent)
        else:
            if recipients:
                campaign.send_cursor = recipients[-1].pk
            campaign.batches_dispatched += 1
            remaining = CampaignRecipient.objects.filter(
                campaign=campaign, id__gt=campaign.send_cursor
            ).exists()
            if remaining:
                outbox.enqueue(
                    topics.CAMPAIGN_SEND_BATCH,
                    {"campaign_id": campaign.pk, "batch_no": batch_no + 1},
                    dedupe_key=topics.campaign_send_batch_key(campaign.pk, batch_no + 1),
                )
            else:
                campaign.state = Campaign.State.SENT
                campaign.sent_at = now
                _refresh_tally(campaign)
            campaign.save()
    if not_sent:
        raise outbox.RetryLater(
            NOT_SENT_RETRY,
            f"campaign {campaign_id} batch {batch_no}: {len(not_sent)} push(es) not sent by FCM",
            give_up_after=NOT_SENT_GIVE_UP,
        )


# --- status pushes (handler bodies of push.order_status / push.appointment) --------------------------------


def _deliver(
    customer: Customer,
    *,
    dedupe_key: str,
    kind: str,
    title: str,
    body: str,
    link: dict[str, Any],
    source_type: str,
    source_id: int,
    language: str,
    template_status: str = "",
    data: dict[str, str],
    topic: str,
) -> InboxNotification:
    """Claim protocol (design 03 §3.6): ``get_or_create`` the inbox row, claim it, call FCM outside the
    transaction, record the outcome. A redelivery finds the row settled and does nothing.

    ``data`` gets the row's ``inbox_id`` (C§2.14.2) and the message the channel/priority of its type. A dead
    token (``unregistered``) prunes the device; ``payload_invalid`` fails the row with ``push_error =
    "payload_invalid"`` and an ERROR log and returns normally (the outbox message ends ``done``), never
    touching the device. ``FcmUnavailable`` gives the claim back and raises (outbox retry); ``not_sent`` gives
    it back and defers the message (``outbox.RetryLater``); any other exception (a soft time limit) fails the
    row ``interrupted`` — never resent, never left ``sending`` — and re-raises.
    """
    with transaction.atomic():
        device = selectors.customer_push_device(customer.pk)
        inbox, _created = InboxNotification.objects.get_or_create(
            dedupe_key=dedupe_key,
            defaults={
                "customer": customer,
                "kind": kind,
                "title": title,
                "body": body,
                "link": link,
                "source_type": source_type,
                "source_id": source_id,
                "template_status": template_status,
                "language": language,
                "push_state": (
                    InboxNotification.PushState.QUEUED if device else InboxNotification.PushState.NO_DEVICE
                ),
            },
        )
        if inbox.push_state != InboxNotification.PushState.QUEUED:
            return inbox
        if device is None:
            inbox.push_state = InboxNotification.PushState.NO_DEVICE
            inbox.save(update_fields=["push_state"])
            return inbox
        inbox.push_state = InboxNotification.PushState.SENDING
        inbox.save(update_fields=["push_state"])
        token, device_id = device.fcm_token, device.pk
    message = _typed_message(token, inbox.title, inbox.body, {**data, "inbox_id": str(inbox.pk)})
    sending = InboxNotification.objects.filter(pk=inbox.pk, push_state=InboxNotification.PushState.SENDING)
    try:
        [result] = get_fcm_port().send([message])
    except FcmUnavailable:
        sending.update(push_state=InboxNotification.PushState.QUEUED)
        raise
    except Exception:
        # an outcome nobody knows (a soft time limit, a bug): failed, never resent, never stuck "sending"
        sending.update(push_state=InboxNotification.PushState.FAILED, push_error=INTERRUPTED)
        raise
    if result.error == NOT_SENT:
        sending.update(push_state=InboxNotification.PushState.QUEUED)
        raise outbox.RetryLater(
            NOT_SENT_RETRY, f"push {inbox.pk} not sent by FCM", give_up_after=NOT_SENT_GIVE_UP
        )
    now = timeutils.now()
    if result.ok:
        inbox.push_state, inbox.pushed_at = InboxNotification.PushState.SENT, now
    else:
        inbox.push_state, inbox.push_error = InboxNotification.PushState.FAILED, result.error
        _after_failure(result, token, device_id, topic, inbox.pk)
    inbox.save(update_fields=["push_state", "pushed_at", "push_error"])
    return inbox


def render_template(template: StatusTemplate, language: str, order_id: int) -> tuple[str, str]:
    """``(body, language used)``: the customer's language, else Uzbek; ``{raqam}`` → the order number."""
    text = getattr(template, f"text_{language}", "") if language in enums.ContentLanguage.values else ""
    used = language if text else enums.ContentLanguage.UZ
    return (text or template.text_uz).replace("{raqam}", str(order_id)), used


def order_status_title(status: str, fulfilment: str, language: str) -> str:
    """The customer label of C§4.3 in the customer's language; a delivered pickup order reads "Olib
    ketildi"."""
    if status == enums.OrderStatus.DELIVERED and fulfilment == enums.FulfilmentMethod.PICKUP:
        return i18n.t("label.order_status_pickup.delivered", language)
    return i18n.t(f"label.order_status.{status}", language)


def deliver_order_status(payload: dict[str, Any]) -> InboxNotification | None:
    """``push.order_status {order_id, status, event_id, customer_id}``; one push per order per status.

    Title = the customer label (C§4.3) in the customer's language, body = the admin template text. An order
    created before the customer's history horizon (``Customer.history_from``) belongs to a previous holder
    of the number: no inbox row, no push.
    """
    status = str(payload["status"])
    if status not in enums.TemplateStatus.values:
        raise ValueError(f"No push template for order status {status!r}")
    customer = Customer.objects.filter(pk=payload["customer_id"]).first()
    if customer is None:
        logger.warning("push.order_status for unknown customer %s", payload["customer_id"])
        return None
    order_id = int(payload["order_id"])
    facts = selectors.order_push_facts(order_id)
    if facts is None:
        logger.warning("push.order_status for unknown order %s", order_id)
        return None
    fulfilment, created_at = facts
    if customer.history_from is not None and created_at < customer.history_from:
        logger.info("push.order_status skipped: order %s precedes the history horizon", order_id)
        return None
    template = StatusTemplate.objects.get(status=status)
    language = i18n.normalize_language(customer.language)
    # the row is the customer's, in the customer's language (C§2.12): the title is the label in that language;
    # only the body falls back to the Uzbek template text when the template has none in it
    body, _body_language = render_template(template, language, order_id)
    return _deliver(
        customer,
        dedupe_key=topics.order_status_inbox_key(order_id, status),
        kind=InboxNotification.Kind.ORDER_STATUS,
        title=order_status_title(status, fulfilment, language),
        body=body,
        link={"kind": "order", "order_id": order_id},
        source_type=InboxNotification.SourceType.ORDER,
        source_id=order_id,
        language=language,
        template_status=status,
        data={"type": enums.PushDataType.ORDER_STATUS, "order_id": str(order_id), "status": status},
        topic=topics.PUSH_ORDER_STATUS,
    )


# The appointment statuses under which a ``push.appointment`` action still describes the row.
STILL_TRUE: dict[str, frozenset[str]] = {
    "confirmed": frozenset(OPEN_STATUSES),
    "rescheduled": frozenset(OPEN_STATUSES),
    "rejected": frozenset({enums.AppointmentStatus.REJECTED}),
    "cancelled": frozenset({enums.AppointmentStatus.CANCELLED}),
}


def deliver_appointment(payload: dict[str, Any]) -> InboxNotification | None:
    """``push.appointment {appointment_id, action, event_id, customer_id, reschedule_no}``; an action that no
    longer matches the appointment's status (``STILL_TRUE``) is skipped."""
    action = str(payload["action"])
    if action not in texts.APPOINTMENT_TEXTS:
        raise ValueError(f"No push text for appointment action {action!r}")
    appointment = appointment_selectors.get_appointment(int(payload["appointment_id"]))
    if appointment is None:
        logger.warning("push.appointment for unknown appointment %s", payload["appointment_id"])
        return None
    still_true = STILL_TRUE.get(action)
    if still_true is not None and appointment.status not in still_true:
        # e.g. a staff confirm whose push is delivered after the customer cancelled: the stale notice would be
        # the newest one the customer sees (a customer cancel sends none) — no inbox row, no push
        logger.info(
            "push.appointment skipped: action %s no longer matches appointment %s (%s)",
            action,
            appointment.pk,
            appointment.status,
        )
        return None
    customer = appointment.customer
    reschedule_no = payload.get("reschedule_no")
    title, body, language = texts.appointment_text(action, customer.language, appointment.start)
    return _deliver(
        customer,
        dedupe_key=topics.appointment_inbox_key(
            appointment.pk, action, int(reschedule_no) if reschedule_no is not None else None
        ),
        kind=InboxNotification.Kind.APPOINTMENT,
        title=title,
        body=body,
        link={"kind": "appointment", "appointment_id": appointment.pk},
        source_type=InboxNotification.SourceType.APPOINTMENT,
        source_id=appointment.pk,
        language=language,
        data={
            "type": enums.PushDataType.APPOINTMENT,
            "appointment_id": str(appointment.pk),
            "action": action,
        },
        topic=topics.PUSH_APPOINTMENT,
    )


def deliver_account_security(payload: dict[str, Any]) -> InboxNotification | None:
    """Handler body of ``push.account_security`` (C§2.14.2, §2.12): one ``general`` inbox row per
    ``event_id`` (``source_type=account``, ``source_id`` = the customer, ``link {"kind": "none"}``, dedupe
    ``account:{customer_id}:{event}:{event_id}``), the visible notice to the current device, then the same
    notice (same ``inbox_id``) to each ``farewell_token`` — the devices a reset or a social completion
    deactivated. The handler blanks those tokens in the stored payload afterwards. No PII: the texts carry
    the provider name only.
    """
    missing = set(account_topics.ACCOUNT_SECURITY_PAYLOAD_KEYS) - set(payload)
    if missing:
        raise ValueError(f"push.account_security payload misses {sorted(missing)}")
    event = str(payload["event"])
    if event not in enums.AccountSecurityEvent.values:
        raise ValueError(f"Unknown account security event {event!r}")
    customer_id = int(payload["customer_id"])
    customer = Customer.objects.filter(pk=customer_id).first()
    if customer is None:
        logger.warning("push.account_security for unknown customer %s", customer_id)
        return None
    provider = str(payload["provider"] or "")
    language = i18n.normalize_language(customer.language)
    brand = PROVIDER_BRANDS.get(provider, provider)
    title = i18n.t(f"push.account_security.{event}.title", language)
    body = i18n.t(f"push.account_security.{event}.body", language, provider=brand)
    data = {"type": enums.PushDataType.ACCOUNT_SECURITY, "event": event, "provider": provider}
    inbox = _deliver(
        customer,
        dedupe_key=account_topics.account_security_inbox_key(customer_id, event, str(payload["event_id"])),
        kind=InboxNotification.Kind.GENERAL,
        title=title,
        body=body,
        link={"kind": enums.InboxLinkKind.NONE},
        source_type=InboxNotification.SourceType.ACCOUNT,
        source_id=customer_id,
        language=language,
        data=data,
        topic=account_topics.PUSH_ACCOUNT_SECURITY,
    )
    current = selectors.customer_push_device(customer_id)
    candidates = list(
        dict.fromkeys(
            str(token)
            for token in payload.get("farewell_tokens") or []
            if token and (current is None or str(token) != current.fcm_token)
        )
    )
    # C§1.9 step 4 (the push.session_replaced rule): a token now held by an active Device row belongs to an
    # install signed in again since the reset (possibly into another account) — it gets no notice and no
    # inbox id that are not its own.
    held = set(
        Device.objects.filter(fcm_token__in=candidates, is_active=True).values_list("fcm_token", flat=True)
    )
    farewell = [token for token in candidates if token not in held]
    if farewell:
        messages = [
            _typed_message(token, title, body, {**data, "inbox_id": str(inbox.pk)}) for token in farewell
        ]
        for token, result in zip(farewell, get_fcm_port().send(messages), strict=True):
            if result.ok:
                continue
            if result.error == UNREGISTERED:
                # a dead token is pruned wherever it still sits (C§2.14.2), as push.session_replaced does
                for device_id in Device.objects.filter(fcm_token=token).values_list("pk", flat=True):
                    account_services.invalidate_fcm_token(device_id, token=token)
            elif result.error == PAYLOAD_INVALID:
                _log_payload_invalid(account_topics.PUSH_ACCOUNT_SECURITY, inbox.pk)
            else:
                logger.info("push.account_security farewell not delivered error=%s", result.error)
    return inbox


# --- customer inbox (customer contract §2.12) ---------------------------------------------------------


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


# --- templates ----------------------------------------------------------------------------------------------


def template_texts(template: StatusTemplate) -> dict[str, str]:
    return {"uz": template.text_uz, "ru": template.text_ru, "en": template.text_en}


@transaction.atomic
def update_template(
    status: str, texts_: dict[str, str], *, actor: Any, request: Any = None
) -> StatusTemplate:
    """``PUT notification-templates/{status}/`` (validated by the serializer); audit the diff."""
    template = StatusTemplate.objects.select_for_update().get(status=status)
    before = template_texts(template)
    template.text_uz, template.text_ru, template.text_en = texts_["uz"], texts_["ru"], texts_["en"]
    template.updated_at = timeutils.now()
    template.updated_by = actor
    template.save()
    after = template_texts(template)
    audit.record(
        action="notification_template.updated",
        entity_type="notification_template",
        entity_id=status,
        actor=actor,
        request=request,
        before={key: value for key, value in before.items() if after[key] != value},
        after={key: value for key, value in after.items() if before[key] != value},
    )
    return template


# --- segments -----------------------------------------------------------------------------------------------


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


@transaction.atomic
def update_segment(
    segment: CustomerSegment, changes: dict[str, Any], *, actor: Any, request: Any = None
) -> CustomerSegment:
    segment = CustomerSegment.objects.select_for_update().get(pk=segment.pk)
    before = _segment_image(segment)
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
    if Campaign.objects.filter(audience_segment=segment, state=Campaign.State.SCHEDULED).exists():
        raise Conflict(MESSAGES["segment_in_use"])
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
    """``POST segments/{id}/members/``: ``bulk_create(ignore_conflicts=True)``; a known member is a no-op."""
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
            raise Conflict(MESSAGES["rule_member"])
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
