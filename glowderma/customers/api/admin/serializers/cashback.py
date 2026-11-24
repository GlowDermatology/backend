"""The admin cashback adjustment of a customer (admin contract §2.6): the request and its answer."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from glowderma.core import enums
from glowderma.customers.api.admin.serializers.card import LedgerEntrySerializer
from glowderma.loyalty.services import ADJUSTMENT_MAX_AMOUNT, ADJUSTMENT_MIN_AMOUNT

REASON_MIN_LENGTH = 3


REASON_MAX_LENGTH = 300


class CashbackAdjustmentSerializer(serializers.Serializer[dict[str, Any]]):
    """``POST customers/{id}/cashback-adjustments/`` body (OpenAPI shape).

    The view validates ``direction`` here and hands ``amount`` / ``reason`` as received to the service, which
    runs the contract checks in order with the table's messages.
    """

    direction = serializers.ChoiceField(choices=enums.AdjustmentDirection.values)
    amount = serializers.IntegerField(min_value=ADJUSTMENT_MIN_AMOUNT, max_value=ADJUSTMENT_MAX_AMOUNT)
    reason = serializers.CharField(min_length=REASON_MIN_LENGTH, max_length=REASON_MAX_LENGTH)


class CashbackAdjustmentDirectionSerializer(serializers.Serializer[dict[str, Any]]):
    direction = serializers.ChoiceField(choices=enums.AdjustmentDirection.values)


class CashbackAdjustmentResultSerializer(serializers.Serializer[Any]):
    """201: the new ``adjustment`` entry, the balance after it and ``available`` (balance − active holds)."""

    entry = LedgerEntrySerializer()
    balance = serializers.IntegerField()
    available = serializers.IntegerField()


__all__ = [
    "REASON_MAX_LENGTH",
    "REASON_MIN_LENGTH",
    "CashbackAdjustmentDirectionSerializer",
    "CashbackAdjustmentResultSerializer",
    "CashbackAdjustmentSerializer",
]
