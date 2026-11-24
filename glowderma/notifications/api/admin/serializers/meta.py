"""The ``notifications/meta/`` answer: segments and link options of the campaign editor."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from glowderma.notifications.api.admin.serializers.status_templates import StatusTemplateSerializer


class MetaSegmentSerializer(serializers.Serializer[dict[str, Any]]):
    id = serializers.IntegerField()
    name = serializers.CharField()
    size = serializers.IntegerField()


class LinkOptionSerializer(serializers.Serializer[dict[str, Any]]):
    product_id = serializers.IntegerField()
    label = serializers.CharField()  # type: ignore[assignment]  # a field, not Field.label


class NotificationMetaSerializer(serializers.Serializer[dict[str, Any]]):
    total_devices = serializers.IntegerField()
    segments = MetaSegmentSerializer(many=True)
    templates = StatusTemplateSerializer(many=True)
    link_options = LinkOptionSerializer(many=True)
    approval_delay_minutes = serializers.IntegerField(
        help_text="CAMPAIGN_APPROVAL_DELAY_MINUTES: the cancel window between an approval and the dispatch."
    )


__all__ = [
    "LinkOptionSerializer",
    "MetaSegmentSerializer",
    "NotificationMetaSerializer",
]
