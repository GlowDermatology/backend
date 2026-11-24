"""What the admin ``catalog`` serializers share: the Billz id and name rules, the strict boolean and the
ordering bodies."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from glowderma.core.api.exceptions import FIELD_MESSAGES

BILLZ_ID_PATTERN = r"^\d{6,14}$"


UZ_NAME_MIN_LENGTH = 2


class StrictBooleanField(serializers.BooleanField):
    """A JSON boolean, or ``true`` / ``false`` (also ``1`` / ``0``) in multipart; anything else — ``"yes"``,
    ``"on"``, a number — is ``invalid`` (contract §2.4 ``remove_image``; review nit: not DRF's truthy set)."""

    default_error_messages = {"invalid": FIELD_MESSAGES["invalid"]}

    def to_internal_value(self, data: Any) -> bool:
        if isinstance(data, bool):
            return data
        if isinstance(data, str) and data in ("true", "1"):
            return True
        if isinstance(data, str) and data in ("false", "0"):
            return False
        self.fail("invalid")


class SlugOrderSerializer(serializers.Serializer[dict[str, Any]]):
    """Body of ``PUT categories/order/``."""

    slugs = serializers.ListField(child=serializers.CharField(max_length=40))  # ``[]`` → mismatch, or no-op


class IdOrderSerializer(serializers.Serializer[dict[str, Any]]):
    """Body of ``PUT banners/order/``."""

    ids = serializers.ListField(child=serializers.IntegerField())  # ``[]`` → mismatch unless no banners


__all__ = [
    "BILLZ_ID_PATTERN",
    "UZ_NAME_MIN_LENGTH",
    "IdOrderSerializer",
    "SlugOrderSerializer",
    "StrictBooleanField",
]
