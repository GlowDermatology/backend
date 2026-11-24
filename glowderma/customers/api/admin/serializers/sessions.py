"""A customer's open session as the admin sees it (B-W1, admin contract §2.6)."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from glowderma.core import enums
from glowderma.core.api.fields import AwareDateTimeField


class AdminCustomerSessionSerializer(serializers.Serializer[Any]):
    """One open session of the customer (``CustomerSession`` without ``current``)."""

    id = serializers.UUIDField()
    device = serializers.CharField()
    platform = serializers.ChoiceField(choices=enums.OrderChannel.values, allow_null=True)
    location = serializers.CharField(help_text='"Tashkent, Tashkent, UZ"; "—" when unknown.')
    created_at = AwareDateTimeField()
    updated_at = AwareDateTimeField()


__all__ = [
    "AdminCustomerSessionSerializer",
]
