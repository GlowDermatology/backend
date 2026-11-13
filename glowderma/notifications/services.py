"""Writes of `notifications` (design 03 §3): campaign drafts, test pushes, approval, cancel / stop and the
batched send, status-push deliveries, templates, segments and manual members.

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
from django.db.models import Count, F, Q, Value
from django.db.models.functions import Coalesce
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from glowderma.accounts import services as account_services
from glowderma.accounts import topics as account_topics
from glowderma.accounts.models import Customer, Device
from glowderma.appointments import selectors as appointment_selectors
from glowderma.appointments.models import OPEN_STATUSES
from glowderma.catalog.models import Product
from glowderma.core import audit, enums, i18n, outbox, pii, timeutils
from glowderma.core.api.exceptions import Conflict, InvalidTransition, ProviderUnavailable, field_error
from glowderma.core.images import ImageError, process_image
from glowderma.core.media import absolute_media_url, media_name
from glowderma.core.models import OutboxMessage
from glowderma.core.variants import delete_with_variants, store_variants
from glowderma.integrations.fcm.ports import (
    NOT_SENT,
    PAYLOAD_INVALID,
    UNREGISTERED,
    FcmUnavailable,
    PushMessage,
    PushResult,
)
from glowderma.integrations.fcm.registry import get_fcm_port
from glowderma.notifications import selectors, state, texts, topics
from glowderma.notifications.media import CAMPAIGN_IMAGE_KIND
from glowderma.notifications.models import (
    Campaign,
    CampaignRecipient,
    CampaignTest,
    CustomerSegment,
    InboxNotification,
    SegmentMember,
    StatusTemplate,
    TestDevice,
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


# --- campaigns: drafts, test pushes, approval (admin contract §2.8) --------------------------------------

# Uzbek messages of the approval workflow (409 ``conflict`` unless noted).
MESSAGE_STALE_REVISION = "Xabar o‘zgartirilgan: sahifani yangilab, qayta urinib ko‘ring."
MESSAGE_NOT_TESTED = state.MESSAGE_NOT_TESTED
MESSAGE_TEST_STALE = "Bu test eski matn uchun yuborilgan: qayta test yuboring."
MESSAGE_TEST_UNDELIVERED = "Bu test qurilmaga yetib bormagan: tasdiqlab bo‘lmaydi."
MESSAGE_NO_TEST_DEVICES = "Push qabul qila oladigan test qurilmasi tanlanmadi."
MESSAGE_SEGMENT_GONE = "Tanlangan segment o‘chirilgan: auditoriyani qayta tanlang."
MESSAGE_FCM_DOWN = "Push xizmati javob bermadi: birozdan so‘ng qayta urinib ko‘ring."
MESSAGE_CANCEL_STARTED = state.MESSAGE_CANCEL_STARTED
MESSAGE_CANCEL_DRAFT = state.MESSAGE_CANCEL_DRAFT
MESSAGE_NOT_SENDING = state.MESSAGE_NOT_SENDING
TEST_DEVICE_MESSAGES = {
    "customer_not_found": "Bu raqam bilan mijoz topilmadi.",
    "device_not_pushable": "Bu mijozning push qabul qiladigan qurilmasi yo‘q.",
    "unique": "Bu qurilma allaqachon ro‘yxatda.",
    "does_not_exist": "Test qurilmasi topilmadi.",
}
STOPPED = "stopped"  # recipient / inbox error of a push never attempted because the campaign was stopped
UNAVAILABLE_TEST = "unavailable"  # a test push FCM did not take at all (the provider was down)
# The content fields whose change bumps ``content_revision`` and clears the test confirmation.
CONTENT_FIELDS = (
    "kind",
    "title",
    "body",
    "link_kind",
    "link_product_id",
    "link_product_label",
    "link_url",
    "audience_type",
    "audience_segment_id",
    "audience_segment_name",
    "audience_customer_id",
    "audience_phone",
)


def _iso(value: Any) -> str | None:
    return timeutils.isoformat(value) if value is not None else None


def approval_delay() -> timedelta:
    """The minimum wait between an approval and the dispatch (``CAMPAIGN_APPROVAL_DELAY_MINUTES``)."""
    return timedelta(minutes=int(settings.CAMPAIGN_APPROVAL_DELAY_MINUTES))


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
    scheduled_at: Any = None  # the author's requested send time (optional)
    extra: dict[str, Any] = field(default_factory=dict)


def _store_image(campaign: Campaign, upload: Any) -> None:
    try:
        processed = process_image(upload)
    except ImageError as exc:
        raise ValidationError({"image": [field_error(exc.code)]}) from exc
    name = media_name(CAMPAIGN_IMAGE_KIND, campaign.pk, processed.content, processed.extension)
    campaign.image.save(name.rsplit("/", 1)[-1], ContentFile(processed.content), save=False)
    try:
        store_variants(campaign.image.storage, campaign.image.name or "", processed.content)
    except BaseException:
        campaign.image.storage.delete(campaign.image.name or "")  # the row rolls back: no orphan original
        raise
    campaign.save(update_fields=["image"])


def _remove_image(campaign: Campaign) -> None:
    if campaign.image:
        _delete_image_files(campaign)
        campaign.save(update_fields=["image"])


def _delete_image_files(campaign: Campaign) -> None:
    """Delete the stored image and its variants now and leave the field empty on the instance."""
    delete_with_variants(campaign.image.storage, campaign.image.name or "")
    campaign.image = ""


def _campaign_audit_image(campaign: Campaign) -> dict[str, Any]:
    return {
        "kind": campaign.kind,
        "title": campaign.title,
        "body": campaign.body,
        "audience_type": campaign.audience_type,
        "audience_segment_id": campaign.audience_segment_id,
        "audience_phone": campaign.audience_phone or None,
        "link_kind": campaign.link_kind,
        "link_product_id": campaign.link_product_id,
        "link_url": campaign.link_url or None,
        "image": (campaign.image.name or None) if campaign.image else None,
        "scheduled_at": timeutils.isoformat(campaign.scheduled_at) if campaign.scheduled_at else None,
        "state": campaign.state,
        "revision": campaign.content_revision,
        "recipient_count": campaign.recipient_count,
    }


def _content_snapshot(campaign: Campaign) -> dict[str, Any]:
    snapshot = {name: getattr(campaign, name) for name in CONTENT_FIELDS}
    snapshot["image"] = campaign.image.name if campaign.image else ""
    return snapshot


def _lock_campaign(campaign_id: int) -> Campaign:
    campaign = Campaign.objects.select_for_update().filter(pk=campaign_id).first()
    if campaign is None:
        raise NotFound
    return campaign


def ensure_can_edit(campaign: Campaign, actor: Any) -> None:
    """Drafts are edited and deleted by their author or by a superuser (admin contract §2.8); anyone else
    gets 403 ``permission_denied``."""
    if getattr(actor, "is_superuser", False):
        return
    if campaign.author_id is None or campaign.author_id != getattr(actor, "pk", None):
        raise PermissionDenied


def _audit(
    campaign: Campaign, action: str, *, actor: Any, request: Any, before: Any = None, after: Any = None
) -> None:
    audit.record(
        action=action,
        entity_type="campaign",
        entity_id=str(campaign.pk),
        actor=actor,
        request=request,
        before=before,
        after=after,
    )


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


def _start(campaign: Campaign) -> None:
    """``scheduled`` → ``sending`` (recipients materialised, live tallies at 0, batch 0 enqueued) or ``sent``
    at once when nobody can be reached; audited ``notification.send_started`` by the system. The caller
    (``start_due_campaigns``) saves the row inside the same transaction."""
    now = timeutils.now()
    before = {"state": campaign.state, "dispatch_after": _iso(campaign.dispatch_after)}
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
    _audit(
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


def _label_snapshot(product: Product | None) -> str:
    """The uz display name at creation, cut to the column: ``billz_name`` (the fallback) holds up to 255."""
    if product is None:
        return ""
    limit = Campaign._meta.get_field("link_product_label").max_length or 120
    return product.display_name[:limit]


def _apply_input(campaign: Campaign, data: CampaignInput) -> None:
    campaign.kind = data.kind
    campaign.title = data.title
    campaign.body = data.body
    campaign.link_kind = data.link_kind
    campaign.link_product = data.link_product
    campaign.link_product_label = _label_snapshot(data.link_product)
    campaign.link_url = data.link_url if data.link_kind == enums.LinkKind.URL else ""
    campaign.audience_type = data.audience_type
    campaign.audience_segment = data.audience_segment
    campaign.audience_segment_name = data.audience_segment.name if data.audience_segment is not None else ""
    campaign.audience_customer = data.audience_customer
    campaign.audience_phone = (
        (data.audience_customer.phone or "") if data.audience_customer is not None else ""
    )
    campaign.scheduled_at = data.scheduled_at


@transaction.atomic
def create_draft(data: CampaignInput, image: Any = None, *, actor: Any, request: Any = None) -> Campaign:
    """``POST notifications/``: a ``draft`` (revision 1) with an estimated ``recipient_count``; nothing is
    materialised, enqueued or pushed. Audit ``notification.created``."""
    campaign = Campaign(state=Campaign.State.DRAFT, author=actor)
    _apply_input(campaign, data)
    campaign.save()
    if image is not None:
        _store_image(campaign, image)
    campaign.recipient_count = len(selectors.audience_recipients(campaign))
    campaign.save()
    _audit(
        campaign,
        state.transition("create").audit_action,
        actor=actor,
        request=request,
        after=_campaign_audit_image(campaign),
    )
    return campaign


def _clear_test_confirmation(campaign: Campaign) -> None:
    campaign.tested_revision = None
    campaign.test_confirmed_at = None
    campaign.test_confirmed_by = None


@transaction.atomic
def update_draft(
    campaign_id: int,
    data: CampaignInput,
    image: Any = None,
    *,
    remove_image: bool = False,
    expected_revision: int,
    actor: Any,
    request: Any = None,
) -> Campaign:
    """``PATCH notifications/{id}/`` (author or superuser): a ``draft`` / ``tested`` campaign takes the merged
    form. A content change (kind, title, body, image, link, audience) bumps ``content_revision``, clears the
    test confirmation and puts a ``tested`` campaign back to ``draft``; a change of the requested time alone
    keeps the state. ``expected_revision`` must be the current revision (409 ``conflict``). Audit
    ``notification.draft_updated`` with the changed fields."""
    campaign = _lock_campaign(campaign_id)
    ensure_can_edit(campaign, actor)
    state.check("update", campaign.state)
    if expected_revision != campaign.content_revision:
        raise Conflict(MESSAGE_STALE_REVISION)
    before_image = _campaign_audit_image(campaign)
    before_content = _content_snapshot(campaign)
    _apply_input(campaign, data)
    campaign.save()
    if remove_image:
        _remove_image(campaign)
    if image is not None:
        _remove_image(campaign)
        _store_image(campaign, image)
    content_changed = _content_snapshot(campaign) != before_content
    if content_changed:
        campaign.content_revision += 1
        campaign.state = Campaign.State.DRAFT
        _clear_test_confirmation(campaign)
    campaign.recipient_count = len(selectors.audience_recipients(campaign))
    campaign.save()
    after_image = _campaign_audit_image(campaign)
    _audit(
        campaign,
        state.transition("update").audit_action,
        actor=actor,
        request=request,
        before={key: value for key, value in before_image.items() if after_image[key] != value},
        after={key: value for key, value in after_image.items() if before_image[key] != value},
    )
    return campaign


@transaction.atomic
def delete_draft(campaign_id: int, *, actor: Any, request: Any = None) -> None:
    """``DELETE notifications/{id}/`` (author or superuser): a ``draft`` / ``tested`` campaign and its image
    file go; its tests cascade. Audit ``notification.draft_deleted``."""
    campaign = _lock_campaign(campaign_id)
    ensure_can_edit(campaign, actor)
    state.check("delete", campaign.state)
    before = _campaign_audit_image(campaign)
    pk = campaign.pk
    if campaign.image:
        _delete_image_files(campaign)
    campaign.delete()
    audit.record(
        action=state.transition("delete").audit_action,
        entity_type="campaign",
        entity_id=str(pk),
        actor=actor,
        request=request,
        before=before,
    )


# --- test devices -------------------------------------------------------------------------------------------


def _test_device_image(test_device: TestDevice) -> dict[str, Any]:
    """The audit image: the audit log is readable by every staff member, the registry (and the full number of
    the superuser's personal phone) by superusers only, so the number is masked here."""
    return {
        "device_id": test_device.device_id,
        "phone": pii.mask_phone(test_device.phone),
        "label": test_device.label,
        "is_active": test_device.is_active,
    }


@transaction.atomic
def register_test_device(*, phone: str, label: str, actor: Any, request: Any = None) -> TestDevice:
    """``POST notifications/test-devices/`` (superuser): the active customer with ``phone`` must have a
    pushable device (its most recently active one is registered). Field errors ``phone``
    ``customer_not_found`` / ``device_not_pushable`` / ``unique``. Audit ``test_device.registered``."""
    customer = Customer.objects.filter(phone=phone, status=enums.CustomerStatus.ACTIVE).first()
    if customer is None:
        raise ValidationError(
            {"phone": [field_error("customer_not_found", TEST_DEVICE_MESSAGES["customer_not_found"])]}
        )
    device = (
        # The row lock orders two registrations of one device: the second waits and sees the duplicate
        # instead of hitting the partial unique index (``testdevice_active_device_unique``).
        Device.objects.select_for_update()
        .filter(selectors.PUSHABLE_DEVICE, customer=customer)
        .order_by("-last_active_at", "-id")
        .first()
    )
    if device is None:
        raise ValidationError(
            {"phone": [field_error("device_not_pushable", TEST_DEVICE_MESSAGES["device_not_pushable"])]}
        )
    if TestDevice.objects.filter(device=device, is_active=True).exists():
        raise ValidationError({"phone": [field_error("unique", TEST_DEVICE_MESSAGES["unique"])]})
    test_device = TestDevice.objects.create(
        device=device, phone=phone, label=label.strip(), registered_by=actor
    )
    audit.record(
        action=state.AUDIT_TEST_DEVICE_REGISTERED,
        entity_type="test_device",
        entity_id=str(test_device.pk),
        actor=actor,
        request=request,
        after=_test_device_image(test_device),
    )
    return test_device


@transaction.atomic
def remove_test_device(test_device_id: int, *, actor: Any, request: Any = None) -> TestDevice:
    """``DELETE notifications/test-devices/{id}/`` (superuser): soft (``is_active = False``), idempotent
    (an inactive row answers unchanged, no second audit); 404 for an unknown id."""
    test_device = TestDevice.objects.select_for_update().filter(pk=test_device_id).first()
    if test_device is None:
        raise NotFound
    if not test_device.is_active:
        return test_device
    test_device.is_active = False
    test_device.save(update_fields=["is_active"])
    audit.record(
        action=state.AUDIT_TEST_DEVICE_REMOVED,
        entity_type="test_device",
        entity_id=str(test_device.pk),
        actor=actor,
        request=request,
        before={"is_active": True},
        after=_test_device_image(test_device),
    )
    return test_device


# --- test pushes and approval -------------------------------------------------------------------------------


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


def _test_token(test_device: TestDevice) -> str:
    """A test push goes only where the registry says ``is_pushable`` (:func:`selectors.test_devices`): the
    device still takes pushes **and** its customer is active (``device__customer`` is loaded)."""
    device = test_device.device
    if device.customer.status != enums.CustomerStatus.ACTIVE:
        return ""
    return _usable_token(device)


def campaign_messages(campaign: Campaign, targets: Iterable[tuple[str, int]]) -> list[PushMessage]:
    """Campaign pushes (C§2.14.2) for ``(token, inbox_id)`` pairs — the one message builder of the batched
    send and of the test pushes: ``data`` ``{type, inbox_id, campaign_id, product_id | "", link_url | ""}``,
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
            token,
            campaign.title,
            campaign.body,
            {
                "type": enums.PushDataType.CAMPAIGN,
                "inbox_id": str(inbox_id),
                "campaign_id": str(campaign.pk),
                "product_id": product_id,
                "link_url": link_url,
            },
            channel=channel,
            priority=CAMPAIGN_PRIORITY,
            image_url=image_url,
        )
        for token, inbox_id in targets
    ]


def _test_image(test: CampaignTest) -> dict[str, Any]:
    return {
        "test_id": test.pk,
        "test_device_id": test.test_device_id,
        "device_id": test.device_id,
        "revision": test.revision,
        "fcm_ok": test.fcm_ok,
        "fcm_error": test.fcm_error or None,
    }


def send_test(
    campaign_id: int, test_device_ids: Sequence[int], *, actor: Any, request: Any = None
) -> Campaign:
    """``POST notifications/{id}/test/`` (superuser): push the current revision of a ``draft`` / ``tested``
    campaign to the chosen active test devices.

    Transaction 1 locks the campaign, checks the state and writes one ``CampaignTest`` (``fcm_ok`` null) per
    device with a real ``InboxNotification`` for the tester (dedupe ``campaign-test:{campaign}:{test}``), so
    the deep link opens in the app; a device that lost its token, or whose customer is no longer active, gets
    ``fcm_ok = False`` ``no_device`` at once (the registry shows it ``is_pushable = false``). The FCM call
    runs **outside** the transaction through :func:`campaign_messages`; transaction 2
    records ``fcm_ok`` / ``fcm_error`` / ``provider_message_id`` and the inbox outcome, and prunes a dead
    token (``unregistered``). ``FcmUnavailable`` → every test ``fcm_ok = False`` ``unavailable`` and 503
    ``provider_unavailable``. Audit ``notification.test_sent``.
    """
    wanted = sorted({int(pk) for pk in test_device_ids})
    with transaction.atomic():
        campaign = _lock_campaign(campaign_id)
        state.check("test", campaign.state)
        devices = {
            row.pk: row
            for row in TestDevice.objects.select_related("device", "device__customer").filter(
                pk__in=wanted, is_active=True
            )
        }
        if len(devices) != len(wanted):
            raise ValidationError(
                {"device_ids": [field_error("does_not_exist", TEST_DEVICE_MESSAGES["does_not_exist"])]}
            )
        now = timeutils.now()
        tests: list[CampaignTest] = []
        targets: list[tuple[CampaignTest, InboxNotification, str]] = []
        for pk in wanted:
            test_device = devices[pk]
            token = _test_token(test_device)
            test = CampaignTest.objects.create(
                campaign=campaign,
                revision=campaign.content_revision,
                test_device=test_device,
                device=test_device.device,
                device_label=test_device.label,
                phone=test_device.phone,
                sent_by=actor,
                sent_at=now,
                fcm_ok=None if token else False,
                fcm_error="" if token else NO_DEVICE,
            )
            tests.append(test)
            if not token:
                continue
            inbox = InboxNotification.objects.create(
                customer_id=test_device.device.customer_id,
                dedupe_key=topics.campaign_test_inbox_key(campaign.pk, test.pk),
                **_inbox_defaults(campaign, InboxNotification.PushState.SENDING),
            )
            test.inbox = inbox
            test.save(update_fields=["inbox"])
            targets.append((test, inbox, token))
        if not targets:
            raise Conflict(MESSAGE_NO_TEST_DEVICES)
    messages = campaign_messages(campaign, [(token, inbox.pk) for _test, inbox, token in targets])
    try:
        results = get_fcm_port().send(messages)
    except FcmUnavailable:
        with transaction.atomic():
            for test, inbox, _token in targets:
                CampaignTest.objects.filter(pk=test.pk).update(fcm_ok=False, fcm_error=UNAVAILABLE_TEST)
                InboxNotification.objects.filter(pk=inbox.pk).update(
                    push_state=InboxNotification.PushState.FAILED, push_error=UNAVAILABLE_TEST
                )
        raise ProviderUnavailable(MESSAGE_FCM_DOWN) from None
    with transaction.atomic():
        campaign = _lock_campaign(campaign_id)
        now = timeutils.now()
        for (test, inbox, token), result in zip(targets, results, strict=True):
            if result.ok:
                test.fcm_ok, test.provider_message_id = True, result.message_id
                inbox.push_state, inbox.pushed_at = InboxNotification.PushState.SENT, now
            else:
                test.fcm_ok, test.fcm_error = False, result.error
                inbox.push_state, inbox.push_error = InboxNotification.PushState.FAILED, result.error
                _after_failure(result, token, test.device_id, topics.CAMPAIGN_SEND_BATCH, inbox.pk)
            test.save(update_fields=["fcm_ok", "fcm_error", "provider_message_id"])
            inbox.save(update_fields=["push_state", "pushed_at", "push_error"])
        _audit(
            campaign,
            state.transition("test").audit_action,
            actor=actor,
            request=request,
            after={"revision": campaign.content_revision, "tests": [_test_image(test) for test in tests]},
        )
        return campaign


@transaction.atomic
def confirm_test(campaign_id: int, test_id: int, *, actor: Any, request: Any = None) -> Campaign:
    """``POST notifications/{id}/tests/{test_id}/confirm/`` (superuser): the superuser saw the test on the
    device. The test must be the current revision's (409 ``conflict`` :data:`MESSAGE_TEST_STALE`) and
    delivered (:data:`MESSAGE_TEST_UNDELIVERED`); the campaign becomes ``tested`` for that revision. A
    confirmed test answers unchanged. Audit ``notification.test_confirmed``."""
    campaign = _lock_campaign(campaign_id)
    test = CampaignTest.objects.filter(pk=test_id, campaign=campaign).first()
    if test is None:
        raise NotFound
    if test.confirmed_at is not None and campaign.tested_revision == campaign.content_revision:
        return campaign
    state.check("confirm", campaign.state)
    if test.revision != campaign.content_revision:
        raise Conflict(MESSAGE_TEST_STALE)
    if test.fcm_ok is not True:
        raise Conflict(MESSAGE_TEST_UNDELIVERED)
    now = timeutils.now()
    before = {"state": campaign.state, "tested_revision": campaign.tested_revision}
    if test.confirmed_at is None:
        test.confirmed_at, test.confirmed_by = now, actor
        test.save(update_fields=["confirmed_at", "confirmed_by"])
    campaign.state = Campaign.State.TESTED
    campaign.tested_revision = campaign.content_revision
    campaign.test_confirmed_at, campaign.test_confirmed_by = now, actor
    campaign.save(update_fields=["state", "tested_revision", "test_confirmed_at", "test_confirmed_by"])
    _audit(
        campaign,
        state.transition("confirm").audit_action,
        actor=actor,
        request=request,
        before=before,
        after={"state": campaign.state, "tested_revision": campaign.tested_revision, "test_id": test.pk},
    )
    return campaign


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
    campaign = _lock_campaign(campaign_id)
    if expected_revision != campaign.content_revision:
        raise Conflict(MESSAGE_STALE_REVISION)
    if campaign.state == Campaign.State.DRAFT:  # the one wrong state with a remedy: test and confirm first
        raise InvalidTransition(MESSAGE_NOT_TESTED)
    if not state.check("approve", campaign.state):
        if campaign.approved_revision == campaign.content_revision:
            return campaign
        raise Conflict(MESSAGE_STALE_REVISION)  # approved for another revision: never happens, never ignored
    if campaign.tested_revision != campaign.content_revision:
        raise Conflict(MESSAGE_NOT_TESTED)
    if campaign.audience_type == enums.AudienceType.SEGMENT and campaign.audience_segment_id is None:
        raise Conflict(MESSAGE_SEGMENT_GONE)
    now = timeutils.now()
    earliest = now + approval_delay()
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
    _audit(
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
    campaign = _lock_campaign(campaign_id)
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
    _clear_test_confirmation(campaign)
    campaign.save()
    _audit(
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
        campaign = _lock_campaign(campaign_id)
        if campaign.state in Campaign.EDITABLE_STATES:
            raise InvalidTransition(MESSAGE_CANCEL_DRAFT)
        if campaign.state == Campaign.State.CANCELLED:
            return campaign
        if campaign.state != Campaign.State.SCHEDULED:
            raise Conflict(MESSAGE_CANCEL_STARTED)
        before = {
            "state": campaign.state,
            "scheduled_at": _iso(campaign.scheduled_at),
            "dispatch_after": _iso(campaign.dispatch_after),
        }
        cancelled_at = timeutils.now()
        campaign.state = Campaign.State.CANCELLED
        campaign.cancelled_at, campaign.cancelled_by = cancelled_at, actor
        campaign.save(update_fields=["state", "cancelled_at", "cancelled_by"])
        _audit(
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
    campaign = _lock_campaign(campaign_id)
    if campaign.stop_requested_at is not None:
        return campaign
    state.check("stop", campaign.state)
    now = timeutils.now()
    campaign.state = Campaign.State.STOPPING
    campaign.stop_requested_at, campaign.stop_requested_by = now, actor
    campaign.save(update_fields=["state", "stop_requested_at", "stop_requested_by"])
    _audit(
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
            _start(campaign)
            campaign.save()
            started.append(pk)
    return started


# --- the batched send ---------------------------------------------------------------------------------------


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
    return campaign_messages(campaign, [(claim.token, claim.inbox.pk) for claim in claims])


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
    # a filtered update: a finalisation that overlapped this delivery may have withdrawn the row already
    InboxNotification.objects.filter(pk=inbox.pk).update(
        push_state=inbox.push_state, pushed_at=inbox.pushed_at, push_error=inbox.push_error
    )


def _stop_claim(claim: _Claim) -> None:
    """A claim FCM did not take while the campaign is stopping: never attempted again (recipient
    ``stopped``, its in-app row withdrawn) — the claim is not given back."""
    recipient = claim.recipient
    recipient.state, recipient.error = CampaignRecipient.State.STOPPED, STOPPED
    recipient.save(update_fields=["state", "error"])
    _withdraw_inbox([claim.inbox.pk])


def _withdraw_inbox(inbox_ids: Sequence[int]) -> None:
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
    pending.update(state=CampaignRecipient.State.STOPPED, error=STOPPED)
    _withdraw_inbox(inbox_ids)


def _after_failure(result: PushResult, token: str, device_id: int | None, topic: str, inbox_id: int) -> None:
    """Token pruning only when FCM says the token is dead (``unregistered``); a malformed message
    (``payload_invalid``) is alerted and never touches a device (C§2.14.2)."""
    if result.error == UNREGISTERED and device_id is not None:
        account_services.invalidate_fcm_token(device_id, token=token)
    elif result.error == PAYLOAD_INVALID:
        _log_payload_invalid(topic, inbox_id)


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
    _audit(
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
        _audit(
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

    B-W1: the push fans out to **every** pushable device of the customer (one FCM batch, newest device
    first, at most ``selectors.MAX_PUSH_DEVICES``), all carrying the same ``inbox_id``. ``data`` gets the
    row's ``inbox_id`` (C§2.14.2) and the message the channel/priority of its type. The row is ``sent`` when
    at least one device got it; a dead token (``unregistered``) prunes that device; ``payload_invalid``
    fails the row with ``push_error = "payload_invalid"`` and an ERROR log and returns normally (the outbox
    message ends ``done``), never touching a device. ``FcmUnavailable`` gives the claim back and raises
    (outbox retry); when no device got it and one answered ``not_sent`` the claim is given back and the
    message deferred (``outbox.RetryLater``); any other exception (a soft time limit) fails the row
    ``interrupted`` — never resent, never left ``sending`` — and re-raises.
    """
    with transaction.atomic():
        devices = selectors.customer_push_devices(customer.pk)
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
                    InboxNotification.PushState.QUEUED if devices else InboxNotification.PushState.NO_DEVICE
                ),
            },
        )
        if inbox.push_state != InboxNotification.PushState.QUEUED:
            return inbox
        if not devices:
            inbox.push_state = InboxNotification.PushState.NO_DEVICE
            inbox.save(update_fields=["push_state"])
            return inbox
        inbox.push_state = InboxNotification.PushState.SENDING
        inbox.save(update_fields=["push_state"])
        targets = list(dict.fromkeys((device.fcm_token, device.pk) for device in devices))
    messages = [
        _typed_message(token, inbox.title, inbox.body, {**data, "inbox_id": str(inbox.pk)})
        for token, _device_id in targets
    ]
    sending = InboxNotification.objects.filter(pk=inbox.pk, push_state=InboxNotification.PushState.SENDING)
    try:
        results = get_fcm_port().send(messages)
    except FcmUnavailable:
        sending.update(push_state=InboxNotification.PushState.QUEUED)
        raise
    except Exception:
        # an outcome nobody knows (a soft time limit, a bug): failed, never resent, never stuck "sending"
        sending.update(push_state=InboxNotification.PushState.FAILED, push_error=INTERRUPTED)
        raise
    now = timeutils.now()
    delivered = any(result.ok for result in results)
    for (token, device_id), result in zip(targets, results, strict=True):
        if not result.ok and result.error != NOT_SENT:
            _after_failure(result, token, device_id, topic, inbox.pk)
    if not delivered and any(result.error == NOT_SENT for result in results):
        sending.update(push_state=InboxNotification.PushState.QUEUED)
        raise outbox.RetryLater(
            NOT_SENT_RETRY, f"push {inbox.pk} not sent by FCM", give_up_after=NOT_SENT_GIVE_UP
        )
    if delivered:
        inbox.push_state, inbox.pushed_at = InboxNotification.PushState.SENT, now
    else:
        inbox.push_state, inbox.push_error = InboxNotification.PushState.FAILED, results[0].error
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
    ``account:{customer_id}:{event}:{event_id}``), the visible notice to every signed-in pushable device
    (B-W1 fan-out), then the same notice (same ``inbox_id``) to each ``farewell_token`` — the devices a reset
    or a password change signed out. The handler blanks those tokens in the stored payload afterwards. No
    PII: the texts carry the provider name only.
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
    current = {device.fcm_token for device in selectors.customer_push_devices(customer_id)}
    candidates = list(
        dict.fromkeys(
            str(token)
            for token in payload.get("farewell_tokens") or []
            if token and str(token) not in current
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


def _ensure_segment_free(segment: CustomerSegment) -> None:
    """409 ``conflict`` while an approved ``scheduled`` campaign targets the segment: its recipients are
    materialised at dispatch, so the audience the superuser approved must not grow meanwhile (through a rule
    change or new members — contract §2.8; the delete is refused for the same reason). Narrowing (removing
    a member) and renaming stay allowed; once the beat started the campaign the segment is free again."""
    if Campaign.objects.filter(audience_segment=segment, state=Campaign.State.SCHEDULED).exists():
        raise Conflict(MESSAGES["segment_in_use"])


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
