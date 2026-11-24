"""The staff test devices: the row and its registration."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from glowderma.core.api.exceptions import field_error
from glowderma.core.api.fields import (
    LenientPhoneField,
)
from glowderma.core.api.serializers import ActorSerializer
from glowderma.notifications.api.admin.serializers.messages import MESSAGES
from glowderma.notifications.models import TestDevice

TEST_DEVICES_MAX = 10


class TestDeviceSerializer(serializers.Serializer[TestDevice]):
    """``GET notifications/test-devices/`` rows (``selectors.test_devices`` annotates ``is_pushable``)."""

    id = serializers.IntegerField()
    phone = serializers.CharField()
    label = serializers.CharField()  # type: ignore[assignment]  # a field, not Field.label
    device_id = serializers.IntegerField()
    platform = serializers.CharField(source="device.platform")
    model = serializers.CharField(source="device.model", allow_blank=True)
    is_pushable = serializers.BooleanField()
    registered_by = ActorSerializer(allow_null=True)
    created_at = serializers.DateTimeField()


class TestDeviceCreateSerializer(serializers.Serializer[dict[str, Any]]):
    """``POST notifications/test-devices/``: the customer's phone and a label for the registry."""

    phone = LenientPhoneField()
    # ``allow_blank``: DRF trims first and would answer ``blank``; the contract wants ``min_length`` with
    # its own copy for an empty or whitespace-only label.
    label = serializers.CharField(  # type: ignore[assignment]  # a field, not Field.label
        max_length=60,
        allow_blank=True,
        error_messages={"max_length": MESSAGES["label_max"]},
    )

    def validate_label(self, value: str) -> str:
        if not value.strip():
            raise serializers.ValidationError([field_error("min_length", MESSAGES["label_min"])])
        return value.strip()


__all__ = [
    "TEST_DEVICES_MAX",
    "TestDeviceCreateSerializer",
    "TestDeviceSerializer",
]
