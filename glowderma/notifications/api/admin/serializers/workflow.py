"""The bodies of the campaign workflow actions: send a test, approve."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from glowderma.core.api.fields import (
    BoundedListField,
)
from glowderma.notifications.api.admin.serializers.device_registry import TEST_DEVICES_MAX


class SendTestInputSerializer(serializers.Serializer[dict[str, Any]]):
    """``POST notifications/{id}/test/``: the active test devices to push the current revision to."""

    device_ids = BoundedListField(
        child=serializers.IntegerField(min_value=1), max_items=TEST_DEVICES_MAX, allow_empty=False
    )


class ApproveInputSerializer(serializers.Serializer[dict[str, Any]]):
    """``POST notifications/{id}/approve/``: the revision the superuser is looking at."""

    expected_revision = serializers.IntegerField(min_value=1)


__all__ = [
    "ApproveInputSerializer",
    "SendTestInputSerializer",
]
