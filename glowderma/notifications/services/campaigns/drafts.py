"""Campaign drafts (admin contract §2.8): create, update and delete."""

from __future__ import annotations

from typing import Any

from django.db import transaction

from glowderma.catalog.models import Product
from glowderma.core import audit, enums
from glowderma.core.api.exceptions import Conflict
from glowderma.notifications import selectors, state
from glowderma.notifications.models import (
    Campaign,
)
from glowderma.notifications.services.campaigns import common, images


def _label_snapshot(product: Product | None) -> str:
    """The uz display name at creation, cut to the column: ``billz_name`` (the fallback) holds up to 255."""
    if product is None:
        return ""
    limit = Campaign._meta.get_field("link_product_label").max_length or 120
    return product.display_name[:limit]


def _apply_input(campaign: Campaign, data: common.CampaignInput) -> None:
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
def create_draft(
    data: common.CampaignInput, image: Any = None, *, actor: Any, request: Any = None
) -> Campaign:
    """``POST notifications/``: a ``draft`` (revision 1) with an estimated ``recipient_count``; nothing is
    materialised, enqueued or pushed. Audit ``notification.created``."""
    campaign = Campaign(state=Campaign.State.DRAFT, author=actor)
    _apply_input(campaign, data)
    campaign.save()
    if image is not None:
        images.store_image(campaign, image)
    campaign.recipient_count = len(selectors.audience_recipients(campaign))
    campaign.save()
    common.audit_campaign(
        campaign,
        state.transition("create").audit_action,
        actor=actor,
        request=request,
        after=images.campaign_audit_image(campaign),
    )
    return campaign


@transaction.atomic
def update_draft(
    campaign_id: int,
    data: common.CampaignInput,
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
    campaign = common.lock_campaign(campaign_id)
    common.ensure_can_edit(campaign, actor)
    state.check("update", campaign.state)
    if expected_revision != campaign.content_revision:
        raise Conflict(common.MESSAGE_STALE_REVISION)
    before_image = images.campaign_audit_image(campaign)
    before_content = common.content_snapshot(campaign)
    _apply_input(campaign, data)
    campaign.save()
    if remove_image:
        images.remove_image(campaign)
    if image is not None:
        images.replace_image(campaign, image)
    content_changed = common.content_snapshot(campaign) != before_content
    if content_changed:
        campaign.content_revision += 1
        campaign.state = Campaign.State.DRAFT
        common.clear_test_confirmation(campaign)
    campaign.recipient_count = len(selectors.audience_recipients(campaign))
    campaign.save()
    after_image = images.campaign_audit_image(campaign)
    common.audit_campaign(
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
    campaign = common.lock_campaign(campaign_id)
    common.ensure_can_edit(campaign, actor)
    state.check("delete", campaign.state)
    before = images.campaign_audit_image(campaign)
    pk = campaign.pk
    if campaign.image:
        images.delete_image_files(campaign)
    campaign.delete()
    audit.record(
        action=state.transition("delete").audit_action,
        entity_type="campaign",
        entity_id=str(pk),
        actor=actor,
        request=request,
        before=before,
    )


__all__ = [
    "create_draft",
    "delete_draft",
    "update_draft",
]
