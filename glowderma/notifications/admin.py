"""Read-only Django admin for the notifications models (design 03 §8). Writes go through the services only."""

from django.contrib import admin

from glowderma.core.admin import register_read_only
from glowderma.notifications.models import (
    Campaign,
    CampaignRecipient,
    CustomerSegment,
    InboxNotification,
    SegmentMember,
    StatusTemplate,
)


@admin.display(description="rules")
def segment_rules(segment: CustomerSegment) -> str:
    """``min_online_orders`` / ``min_lifetime_amount`` with ``null`` for a rule that is off."""
    orders = "null" if segment.min_online_orders is None else str(segment.min_online_orders)
    amount = "null" if segment.min_lifetime_amount is None else str(segment.min_lifetime_amount)
    return f"orders ≥ {orders} · lifetime ≥ {amount}"


register_read_only(
    StatusTemplate,
    list_display=("status", "text_uz", "updated_at", "updated_by"),
    search_fields=("status", "text_uz"),
    ordering=("id",),
    raw_id_fields=("updated_by",),
)
register_read_only(
    CustomerSegment,
    list_display=("id", "name", segment_rules, "created_at"),
    search_fields=("name",),
    ordering=("id",),
    raw_id_fields=("created_by",),
)
register_read_only(
    SegmentMember,
    list_display=("id", "segment", "customer", "added_by", "added_at"),
    list_filter=("segment",),
    search_fields=("customer__phone",),
    ordering=("segment_id", "added_at", "id"),
    raw_id_fields=("segment", "customer", "added_by"),
)
register_read_only(
    Campaign,
    list_display=(
        "id",
        "kind",
        "title",
        "audience_type",
        "state",
        "scheduled_at",
        "sent_at",
        "sent",
        "failed",
    ),
    list_filter=("kind", "state", "audience_type"),
    search_fields=("title",),
    ordering=("-id",),
    raw_id_fields=("link_product", "audience_segment", "audience_customer", "author"),
)
register_read_only(
    CampaignRecipient,
    list_display=("id", "campaign", "customer", "device", "state", "sent_at"),
    list_filter=("state",),
    search_fields=("campaign__id",),
    ordering=("campaign_id", "id"),
    raw_id_fields=("campaign", "customer", "device", "inbox"),
)
register_read_only(
    InboxNotification,
    list_display=("id", "customer", "kind", "title", "push_state", "created_at", "read_at"),
    list_filter=("kind", "push_state", "source_type"),
    search_fields=("dedupe_key", "customer__phone"),
    ordering=("-created_at", "-id"),
    raw_id_fields=("customer",),
)
