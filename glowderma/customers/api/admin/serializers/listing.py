"""The admin customer list query (admin contract §2.6)."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from glowderma.core import enums
from glowderma.customers.selectors import DEFAULT_ORDERING, ORDERINGS, SEARCH_MAX_LENGTH


class CustomerListQuerySerializer(serializers.Serializer[dict[str, Any]]):
    search = serializers.CharField(required=False, allow_blank=True, max_length=SEARCH_MAX_LENGTH)
    status = serializers.ChoiceField(choices=enums.CustomerStatus.values, required=False)
    segment = serializers.IntegerField(required=False, min_value=1)
    ordering = serializers.ChoiceField(choices=list(ORDERINGS), required=False, default=DEFAULT_ORDERING)


__all__ = [
    "CustomerListQuerySerializer",
]
