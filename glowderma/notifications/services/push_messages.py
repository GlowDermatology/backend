"""The FCM push message of every notification: the Android channel and priority per ``data.type``, the
retry window of messages FCM did not take and the shared delivery states."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from django.conf import settings

from glowderma.accounts import services as account_services
from glowderma.accounts.models import Device
from glowderma.core import enums
from glowderma.core.media import absolute_media_url
from glowderma.core.uzsource import N_
from glowderma.integrations.fcm.ports import (
    PAYLOAD_INVALID,
    UNREGISTERED,
    PushMessage,
    PushResult,
)
from glowderma.notifications.models import (
    Campaign,
    InboxNotification,
)

logger = logging.getLogger("glowderma.notifications.services")

MESSAGES = {
    "segment_in_use": N_("Rejalashtirilgan xabar bu guruhga yuboriladi."),
    "rule_member": N_("Mijoz guruhga qoida orqali kiradi."),
    "unknown_placeholder": N_("Faqat {raqam} o‘rinbosari ishlatiladi."),
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


def typed_message(token: str, title: str, body: str, data: dict[str, str]) -> PushMessage:
    channel, priority = PUSH_PROFILES[data["type"]]
    return push_message(token, title, body, data, channel=channel, priority=priority)


def log_payload_invalid(topic: str, inbox_id: int | None) -> None:
    """A malformed message (C§2.14.2): alert and fail the message; the device is never touched."""
    logger.error("push.payload_invalid topic=%s field=message inbox_id=%s", topic, inbox_id)


def batch_size() -> int:
    return int(settings.CAMPAIGN_BATCH_SIZE)


def inbox_defaults(campaign: Campaign, push_state: str) -> dict[str, Any]:
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


def usable_token(device: Device | None) -> str:
    if device is None or not device.is_active or not device.push_enabled or not device.fcm_token:
        return ""
    return device.fcm_token


def after_failure(result: PushResult, token: str, device_id: int | None, topic: str, inbox_id: int) -> None:
    """Token pruning only when FCM says the token is dead (``unregistered``); a malformed message
    (``payload_invalid``) is alerted and never touches a device (C§2.14.2)."""
    if result.error == UNREGISTERED and device_id is not None:
        account_services.invalidate_fcm_token(device_id, token=token)
    elif result.error == PAYLOAD_INVALID:
        log_payload_invalid(topic, inbox_id)


__all__ = [
    "after_failure",
    "CAMPAIGN_PRIORITY",
    "IMAGE_MESSAGES",
    "INTERRUPTED",
    "MESSAGES",
    "NOT_SENT_GIVE_UP",
    "NOT_SENT_RETRY",
    "NO_DEVICE",
    "PROVIDER_BRANDS",
    "PUSH_PROFILES",
    "batch_size",
    "inbox_defaults",
    "log_payload_invalid",
    "push_message",
    "typed_message",
    "usable_token",
]
