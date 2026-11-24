"""The admin NotificationRecord: the campaign row with its link, audience and test pushes."""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from glowderma.core import enums
from glowderma.core.api.serializers import ActorSerializer
from glowderma.core.media import absolute_media_url, optional_variant_url
from glowderma.notifications.models import Campaign, CampaignTest


class NotificationLinkSerializer(serializers.Serializer[dict[str, Any]]):
    kind = serializers.ChoiceField(choices=enums.LinkKind.values)
    product_id = serializers.IntegerField(allow_null=True)
    label = serializers.CharField(allow_null=True)  # type: ignore[assignment]  # a field, not Field.label
    url = serializers.CharField(allow_null=True, help_text="Kind `url` (gap ADM-10.1): the https link.")


class NotificationAudienceSerializer(serializers.Serializer[dict[str, Any]]):
    type = serializers.ChoiceField(choices=enums.AudienceType.values)
    segment_id = serializers.IntegerField(allow_null=True)
    segment_name = serializers.CharField(allow_null=True)
    phone = serializers.CharField(allow_null=True)


def record_link(campaign: Campaign) -> dict[str, Any]:
    return {
        "kind": campaign.link_kind,
        "product_id": campaign.link_product_id,
        "label": campaign.link_product_label or None,
        "url": campaign.link_url or None,
    }


def record_audience(campaign: Campaign) -> dict[str, Any]:
    return {
        "type": campaign.audience_type,
        "segment_id": campaign.audience_segment_id,
        "segment_name": campaign.audience_segment_name or None,
        "phone": campaign.audience_phone or None,
    }


class CampaignTestSerializer(serializers.Serializer[CampaignTest]):
    """``NotificationRecord.tests[]``: one test push of a revision to one test device."""

    id = serializers.IntegerField()
    revision = serializers.IntegerField()
    test_device_id = serializers.IntegerField(allow_null=True)
    device_label = serializers.CharField(
        help_text="The registry label; the tester's phone stays in the superuser-only registry."
    )
    sent_at = serializers.DateTimeField()
    sent_by = ActorSerializer(allow_null=True)
    fcm_ok = serializers.BooleanField(
        allow_null=True, help_text="null while the push is in flight; true = FCM accepted it."
    )
    fcm_error = serializers.CharField(allow_blank=True)
    confirmed_at = serializers.DateTimeField(allow_null=True)
    confirmed_by = ActorSerializer(allow_null=True)


class NotificationRecordSerializer(serializers.Serializer[Campaign]):
    id = serializers.IntegerField()
    kind = serializers.ChoiceField(choices=enums.NotificationKind.values)
    title = serializers.CharField()
    body = serializers.CharField()
    image_url = serializers.SerializerMethodField()
    image_thumbnail_url = serializers.SerializerMethodField(help_text="400 px WebP variant.")
    image_medium_url = serializers.SerializerMethodField(help_text="1000 px WebP variant.")
    link = serializers.SerializerMethodField()
    audience = serializers.SerializerMethodField()
    recipient_count = serializers.IntegerField()
    state = serializers.ChoiceField(choices=enums.NotificationState.values)
    scheduled_at = serializers.DateTimeField(
        allow_null=True, help_text="The author's requested send time; the dispatch waits for it."
    )
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()
    revision = serializers.IntegerField(
        source="content_revision", help_text="Bumps on every content change; tests and approvals name it."
    )
    tested_revision = serializers.IntegerField(allow_null=True)
    test_confirmed_at = serializers.DateTimeField(allow_null=True)
    test_confirmed_by = ActorSerializer(allow_null=True)
    approved_at = serializers.DateTimeField(allow_null=True)
    approved_by = ActorSerializer(allow_null=True)
    approved_revision = serializers.IntegerField(allow_null=True)
    dispatch_after = serializers.DateTimeField(
        allow_null=True, help_text="State `scheduled`: the beat starts the send after this instant."
    )
    started_at = serializers.DateTimeField(allow_null=True)
    sent_at = serializers.DateTimeField(allow_null=True)
    cancelled_at = serializers.DateTimeField(
        allow_null=True, help_text="When a superuser cancelled the approved campaign, or a stop sent nothing."
    )
    cancelled_by = ActorSerializer(allow_null=True)
    stop_requested_at = serializers.DateTimeField(allow_null=True)
    stop_requested_by = ActorSerializer(allow_null=True)
    stopped_at = serializers.DateTimeField(allow_null=True)
    failed_at = serializers.DateTimeField(allow_null=True)
    last_error = serializers.CharField(allow_blank=True)
    sent = serializers.IntegerField(allow_null=True, help_text="FCM accepted; a live tally while sending.")
    failed = serializers.IntegerField(
        allow_null=True, help_text="FCM rejected / no token; a live tally while sending."
    )
    attempted = serializers.SerializerMethodField()
    delivered = serializers.IntegerField(allow_null=True)
    author = ActorSerializer(allow_null=True)
    tests = CampaignTestSerializer(many=True)

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_image_url(self, obj: Campaign) -> str | None:
        return absolute_media_url(obj.image.name or "") if obj.image else None

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_image_thumbnail_url(self, obj: Campaign) -> str | None:
        return optional_variant_url(obj.image.name, "thumbnail") if obj.image else None

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_image_medium_url(self, obj: Campaign) -> str | None:
        return optional_variant_url(obj.image.name, "medium") if obj.image else None

    @extend_schema_field(NotificationLinkSerializer())
    def get_link(self, obj: Campaign) -> dict[str, Any]:
        return record_link(obj)

    @extend_schema_field(NotificationAudienceSerializer())
    def get_audience(self, obj: Campaign) -> dict[str, Any]:
        return record_audience(obj)

    @extend_schema_field(serializers.IntegerField(allow_null=True))
    def get_attempted(self, obj: Campaign) -> int | None:
        """``sent + failed`` once the send started (stopped recipients count in neither), else null."""
        if obj.sent is None or obj.failed is None:
            return None
        return obj.sent + obj.failed


__all__ = [
    "CampaignTestSerializer",
    "NotificationAudienceSerializer",
    "NotificationLinkSerializer",
    "NotificationRecordSerializer",
    "record_audience",
    "record_link",
]
