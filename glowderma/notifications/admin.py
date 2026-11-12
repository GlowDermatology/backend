"""Read-only Django admin for the notifications models (design 03 §8). Writes go through the services only."""

from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _

from glowderma.core.admin import (
    DANGER,
    INFO,
    PRIMARY,
    SUCCESS,
    WARNING,
    customer_column,
    date_range,
    display,
    dropdown,
    register_read_only,
    som,
    status_column,
)
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

CAMPAIGN_STATE_VARIANTS = {
    "draft": INFO,
    "tested": INFO,
    "scheduled": PRIMARY,
    "sending": PRIMARY,
    "stopping": WARNING,
    "sent": SUCCESS,
    "partially_sent": WARNING,
    "cancelled": DANGER,
    "failed": DANGER,
}
RECIPIENT_STATE_VARIANTS = {"pending": INFO, "sent": SUCCESS, "failed": DANGER, "stopped": WARNING}
PUSH_STATE_VARIANTS = {"queued": INFO, "sending": PRIMARY, "sent": SUCCESS, "failed": DANGER}


@display(description=_("Qoidalar"))
def segment_rules(segment: CustomerSegment) -> str:
    """``onlayn buyurtmalar ≥ 3 · umrbod xarid ≥ 1 000 000 so‘m``; a rule that is off reads «yo‘q»."""
    orders = gettext("yo‘q") if segment.min_online_orders is None else f"≥ {segment.min_online_orders}"
    amount = (
        gettext("yo‘q") if segment.min_lifetime_amount is None else f"≥ {som(segment.min_lifetime_amount)}"
    )
    return gettext("onlayn buyurtmalar %(orders)s · umrbod xarid %(amount)s") % {
        "orders": orders,
        "amount": amount,
    }


register_read_only(
    StatusTemplate,
    list_display=("status", "text_uz", "updated_at", "updated_by"),
    search_fields=("status", "text_uz"),
    ordering=("id",),
    autocomplete_fields=("updated_by",),
)
register_read_only(
    CustomerSegment,
    title_format=_("Segment: {obj.name}"),
    list_display=("id", "name", segment_rules, "created_at"),
    search_fields=("name",),
    ordering=("id",),
    autocomplete_fields=("created_by",),
)
register_read_only(
    SegmentMember,
    list_display=("id", "segment", customer_column(), "added_by", "added_at"),
    list_filter=("segment",),
    list_select_related=("segment", "customer", "added_by"),
    search_fields=("customer__phone",),
    ordering=("segment_id", "added_at", "id"),
    autocomplete_fields=("segment", "customer", "added_by"),
)
register_read_only(
    Campaign,
    title_format=_("Kampaniya #{obj.pk}: {obj.title}"),
    list_display=(
        "id",
        "kind",
        "title",
        "audience_type",
        status_column("state", _("Holat"), CAMPAIGN_STATE_VARIANTS),
        "scheduled_at",
        "approved_at",
        "dispatch_after",
        "sent_at",
        "sent",
        "failed",
    ),
    list_filter=(dropdown("kind"), dropdown("state"), dropdown("audience_type"), date_range("sent_at")),
    search_fields=("title",),
    ordering=("-id",),
    autocomplete_fields=(
        "link_product",
        "audience_segment",
        "audience_customer",
        "author",
        "test_confirmed_by",
        "approved_by",
        "cancelled_by",
        "stop_requested_by",
    ),
)
register_read_only(
    TestDevice,
    title_format=_("Test qurilmasi: {obj.label}"),
    list_display=("id", "label", "phone", "device", "is_active", "registered_by", "created_at"),
    list_filter=("is_active",),
    list_select_related=("device", "registered_by"),
    search_fields=("label", "phone"),
    ordering=("id",),
    autocomplete_fields=("device", "registered_by"),
)
register_read_only(
    CampaignTest,
    list_display=(
        "id",
        "campaign",
        "revision",
        "device_label",
        "phone",
        "fcm_ok",
        "sent_at",
        "confirmed_at",
    ),
    list_filter=("fcm_ok",),
    list_select_related=("campaign", "test_device", "sent_by", "confirmed_by"),
    search_fields=("campaign__id", "device_label", "phone"),
    ordering=("campaign_id", "id"),
    autocomplete_fields=("campaign", "test_device", "device", "inbox", "sent_by", "confirmed_by"),
)
register_read_only(
    CampaignRecipient,
    list_display=(
        "id",
        "campaign",
        customer_column(),
        "device",
        status_column("state", _("Holat"), RECIPIENT_STATE_VARIANTS),
        "sent_at",
    ),
    list_filter=(dropdown("state"),),
    list_select_related=("campaign", "customer", "device"),
    search_fields=("campaign__id",),
    ordering=("campaign_id", "id"),
    autocomplete_fields=("campaign", "customer", "device", "inbox"),
)
register_read_only(
    InboxNotification,
    list_display=(
        "id",
        customer_column(),
        "kind",
        "title",
        status_column("push_state", _("Push"), PUSH_STATE_VARIANTS),
        "created_at",
        "read_at",
    ),
    list_filter=(dropdown("kind"), dropdown("push_state"), dropdown("source_type"), date_range("created_at")),
    list_select_related=("customer",),
    search_fields=("dedupe_key", "customer__phone"),
    ordering=("-created_at", "-id"),
    autocomplete_fields=("customer",),
)
