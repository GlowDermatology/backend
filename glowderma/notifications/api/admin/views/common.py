"""What the admin ``notifications`` views share: number and phone formats, the audience and link labels, a
fresh NotificationRecord and the segment lookup."""

from __future__ import annotations

from typing import Any

from rest_framework.exceptions import NotFound

from glowderma.core import enums
from glowderma.core.uzsource import N_, tr
from glowderma.notifications import selectors
from glowderma.notifications.api.admin.serializers import (
    NotificationRecordSerializer,
)
from glowderma.notifications.models import Campaign, CustomerSegment

NBSP = " "


def format_number(value: int) -> str:
    """The FE ``formatNumber``: NBSP thousands separator."""
    return f"{value:,}".replace(",", NBSP)


def format_phone(phone: str) -> str:
    """``+998901234567`` → ``+998 90 123 45 67`` (the FE ``formatPhone``)."""
    if len(phone) == 13 and phone.startswith("+998"):
        return f"{phone[:4]} {phone[4:6]} {phone[6:9]} {phone[9:11]} {phone[11:]}"
    return phone


def audience_label(campaign: Campaign) -> str:
    """Contract §5.5 ``audienceLabel``."""
    if campaign.audience_type == enums.AudienceType.ALL:
        return tr(N_("Barcha · %(count)s")) % {"count": format_number(campaign.recipient_count)}
    if campaign.audience_type == enums.AudienceType.SEGMENT:
        return f"{campaign.audience_segment_name} · {format_number(campaign.recipient_count)}"
    return tr(N_("Bitta mijoz · %(phone)s")) % {"phone": format_phone(campaign.audience_phone)}


def link_label(campaign: Campaign) -> str:
    """Export column ``Havola`` (gap ADM-10.1): ``mahsulot #<id>`` (else the product name kept at creation),
    the URL itself for kind ``url``, else empty — like the banners export."""
    if campaign.link_kind == enums.LinkKind.URL:
        return campaign.link_url
    if campaign.link_kind == enums.LinkKind.PRODUCT:
        return (
            tr(N_("mahsulot #%(id)s")) % {"id": campaign.link_product_id}
            if campaign.link_product_id
            else campaign.link_product_label
        )
    return ""


def record_body(campaign_id: int) -> Any:
    return NotificationRecordSerializer(selectors.get_campaign(campaign_id)).data


def segment_or_404(pk: int) -> CustomerSegment:
    segment = CustomerSegment.objects.filter(pk=pk).first()
    if segment is None:
        raise NotFound
    return segment


__all__ = [
    "NBSP",
    "audience_label",
    "format_number",
    "format_phone",
    "link_label",
    "record_body",
    "segment_or_404",
]
