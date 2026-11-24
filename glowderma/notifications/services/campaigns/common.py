"""What the campaign writes share: the workflow refusal texts, the content fields, the input, the campaign
lock and the audit row."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from django.conf import settings
from rest_framework.exceptions import NotFound, PermissionDenied

from glowderma.accounts.models import Customer
from glowderma.catalog.models import Product
from glowderma.core import audit, enums, timeutils
from glowderma.core.media import absolute_media_url
from glowderma.core.uzsource import N_
from glowderma.integrations.fcm.ports import (
    PushMessage,
)
from glowderma.notifications import state
from glowderma.notifications.models import (
    Campaign,
    CustomerSegment,
)
from glowderma.notifications.services import push_messages

# Uzbek messages of the approval workflow (409 ``conflict`` unless noted).
MESSAGE_STALE_REVISION = N_("Xabar o‘zgartirilgan: sahifani yangilab, qayta urinib ko‘ring.")


MESSAGE_NOT_TESTED = state.MESSAGE_NOT_TESTED


MESSAGE_TEST_STALE = N_("Bu test eski matn uchun yuborilgan: qayta test yuboring.")


MESSAGE_TEST_UNDELIVERED = N_("Bu test qurilmaga yetib bormagan: tasdiqlab bo‘lmaydi.")


MESSAGE_NO_TEST_DEVICES = N_("Push qabul qila oladigan test qurilmasi tanlanmadi.")


MESSAGE_SEGMENT_GONE = N_("Tanlangan segment o‘chirilgan: auditoriyani qayta tanlang.")


MESSAGE_FCM_DOWN = N_("Push xizmati javob bermadi: birozdan so‘ng qayta urinib ko‘ring.")


MESSAGE_CANCEL_STARTED = state.MESSAGE_CANCEL_STARTED


MESSAGE_CANCEL_DRAFT = state.MESSAGE_CANCEL_DRAFT


MESSAGE_NOT_SENDING = state.MESSAGE_NOT_SENDING


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


def iso(value: Any) -> str | None:
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


def content_snapshot(campaign: Campaign) -> dict[str, Any]:
    snapshot = {name: getattr(campaign, name) for name in CONTENT_FIELDS}
    snapshot["image"] = campaign.image.name if campaign.image else ""
    return snapshot


def lock_campaign(campaign_id: int) -> Campaign:
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


def audit_campaign(
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


def clear_test_confirmation(campaign: Campaign) -> None:
    campaign.tested_revision = None
    campaign.test_confirmed_at = None
    campaign.test_confirmed_by = None


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
        push_messages.push_message(
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
            priority=push_messages.CAMPAIGN_PRIORITY,
            image_url=image_url,
        )
        for token, inbox_id in targets
    ]


__all__ = [
    "CONTENT_FIELDS",
    "MESSAGE_CANCEL_DRAFT",
    "MESSAGE_CANCEL_STARTED",
    "MESSAGE_FCM_DOWN",
    "MESSAGE_NOT_SENDING",
    "MESSAGE_NOT_TESTED",
    "MESSAGE_NO_TEST_DEVICES",
    "MESSAGE_SEGMENT_GONE",
    "MESSAGE_STALE_REVISION",
    "MESSAGE_TEST_STALE",
    "MESSAGE_TEST_UNDELIVERED",
    "STOPPED",
    "UNAVAILABLE_TEST",
    "CampaignInput",
    "approval_delay",
    "audit_campaign",
    "campaign_messages",
    "clear_test_confirmation",
    "content_snapshot",
    "ensure_can_edit",
    "iso",
    "lock_campaign",
]
