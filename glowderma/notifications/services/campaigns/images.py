"""A campaign's image: stored with its variants, replaced, removed, and its audit snapshot."""

from __future__ import annotations

from typing import Any

from django.core.files.base import ContentFile
from django.db import transaction
from rest_framework.exceptions import ValidationError

from glowderma.core import timeutils
from glowderma.core.api.exceptions import field_error
from glowderma.core.images import ImageError, process_image
from glowderma.core.media import media_name
from glowderma.core.variants import delete_with_variants, store_variants
from glowderma.notifications.media import CAMPAIGN_IMAGE_KIND
from glowderma.notifications.models import (
    Campaign,
)


def store_image(campaign: Campaign, upload: Any) -> None:
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


def replace_image(campaign: Campaign, upload: Any) -> None:
    """Store ``upload`` (validated first) as the campaign image; the previous file and its variants are
    removed after the commit, so a rejected upload or a rollback leaves the current image whole."""
    storage, previous = campaign.image.storage, (campaign.image.name or "") if campaign.image else ""
    store_image(campaign, upload)
    if previous and previous != campaign.image.name:
        transaction.on_commit(lambda: delete_with_variants(storage, previous))


def remove_image(campaign: Campaign) -> None:
    if campaign.image:
        delete_image_files(campaign)
        campaign.save(update_fields=["image"])


def delete_image_files(campaign: Campaign) -> None:
    """Delete the stored image and its variants now and leave the field empty on the instance."""
    delete_with_variants(campaign.image.storage, campaign.image.name or "")
    campaign.image = ""


def campaign_audit_image(campaign: Campaign) -> dict[str, Any]:
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


__all__ = [
    "campaign_audit_image",
    "delete_image_files",
    "remove_image",
    "replace_image",
    "store_image",
]
