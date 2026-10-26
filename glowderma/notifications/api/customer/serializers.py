"""Shapes of the customer inbox routes (customer contract §2.12)."""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from glowderma.core import enums
from glowderma.core.api.exceptions import field_error
from glowderma.core.api.fields import BoundedListField
from glowderma.notifications.models import InboxNotification

LINK_IDS = ("product_id", "order_id", "appointment_id")


class InboxLinkSerializer(serializers.Serializer[Any]):
    kind = serializers.ChoiceField(choices=enums.InboxLinkKind.values)
    product_id = serializers.IntegerField(allow_null=True)
    order_id = serializers.IntegerField(allow_null=True)
    appointment_id = serializers.IntegerField(allow_null=True)


def inbox_link(stored: Any) -> dict[str, Any]:
    """The stored ``link`` JSON in the C§2.12 shape: every id key present (``null`` when absent)."""
    data = stored if isinstance(stored, dict) else {}
    kind = data.get("kind")
    if kind not in enums.InboxLinkKind.values:
        kind = enums.InboxLinkKind.NONE
    return {"kind": kind, **{key: data.get(key) for key in LINK_IDS}}


class InboxItemSerializer(serializers.Serializer[InboxNotification]):
    """C§2.12 ``InboxItem``: texts as rendered at creation (never re-rendered after a language switch)."""

    id = serializers.IntegerField()
    kind = serializers.ChoiceField(choices=enums.InboxKind.values)
    title = serializers.CharField()
    body = serializers.CharField()
    image_url = serializers.SerializerMethodField()
    link = serializers.SerializerMethodField()
    created_at = serializers.DateTimeField()
    read_at = serializers.DateTimeField(allow_null=True)
    delivered_at = serializers.DateTimeField(allow_null=True)

    def get_image_url(self, obj: InboxNotification) -> str | None:
        return obj.image_url or None

    @extend_schema_field(InboxLinkSerializer)
    def get_link(self, obj: InboxNotification) -> dict[str, Any]:
        return inbox_link(obj.link)


class UnreadCountSerializer(serializers.Serializer[Any]):
    unread_count = serializers.IntegerField()


class ReadSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    read_at = serializers.DateTimeField()
    unread_count = serializers.IntegerField()


class ReadAllSerializer(serializers.Serializer[Any]):
    updated = serializers.IntegerField()
    unread_count = serializers.IntegerField()


class AckRequestSerializer(serializers.Serializer[dict[str, Any]]):
    inbox_ids = BoundedListField(
        child=serializers.IntegerField(min_value=1, max_value=2**63 - 1), max_items=50
    )

    def validate_inbox_ids(self, value: list[int]) -> list[int]:
        if not value:  # 1..50 ids: an empty list is a missing value (C§2.12)
            raise serializers.ValidationError(field_error("required"))
        return value


class AckSerializer(serializers.Serializer[Any]):
    acknowledged = serializers.IntegerField()
