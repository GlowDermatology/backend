"""``me/sessions/*`` shapes (B-W1, customer contract §2.2.10): one signed-in device and the count a bulk
sign-out revoked."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from glowderma.core import enums
from glowderma.core.api.fields import AwareDateTimeField


class CustomerSessionSerializer(serializers.Serializer[Any]):
    """One signed-in device of the customer (``CustomerSession``)."""

    id = serializers.UUIDField()
    device = serializers.CharField(help_text='"Chrome (Windows)", "Glow Derma iOS (iPhone 15)".')
    platform = serializers.ChoiceField(choices=enums.OrderChannel.values, allow_null=True)
    location = serializers.CharField(help_text='"Tashkent, Tashkent, UZ"; "—" when unknown.')
    created_at = AwareDateTimeField(help_text="The sign-in.")
    updated_at = AwareDateTimeField(help_text="The last activity (recorded at most every 5 minutes).")
    current = serializers.BooleanField(help_text="This request's own session.")


class RevokedSessionsSerializer(serializers.Serializer[Any]):
    revoked = serializers.IntegerField(min_value=0)


__all__ = [
    "CustomerSessionSerializer",
    "RevokedSessionsSerializer",
]
