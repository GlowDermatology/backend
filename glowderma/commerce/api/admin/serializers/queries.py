"""The admin order list and status-count queries and the counts answer."""

from __future__ import annotations

from rest_framework import serializers

from glowderma.commerce import delivery_admin
from glowderma.core import enums
from glowderma.payments.api.admin.serializers import PeriodQuerySerializer


class OrderCountsQuerySerializer(PeriodQuerySerializer):
    """``GET orders/status-counts/``: the ``orders/`` params except ``status``, ``ordering`` and paging."""

    fulfilment = serializers.ChoiceField(choices=enums.FulfilmentMethod.values, required=False)
    payment = serializers.ChoiceField(choices=enums.PaymentStatus.values, required=False)
    customer = serializers.IntegerField(required=False, min_value=1)
    provider = serializers.ChoiceField(
        choices=enums.DeliveryProvider.values, required=False, help_text="The shipment's carrier."
    )
    stage = serializers.ChoiceField(
        choices=enums.ShipmentStage.values, required=False, help_text="A Yandex shipment's stage."
    )
    attention = serializers.ChoiceField(
        choices=list(delivery_admin.ATTENTION_VALUES),
        required=False,
        help_text="`open`: the order has an open delivery issue; `urgent`: an open urgent one.",
    )


class OrderListQuerySerializer(OrderCountsQuerySerializer):
    status = serializers.ChoiceField(choices=enums.OrderStatus.values, required=False)
    ordering = serializers.ChoiceField(
        choices=["-created_at", "created_at"], required=False, default="-created_at"
    )


class StatusCountsSerializer(serializers.Serializer[dict[str, int]]):
    all = serializers.IntegerField()
    awaiting_payment = serializers.IntegerField()
    processing = serializers.IntegerField()
    handed_to_courier = serializers.IntegerField()
    delivered = serializers.IntegerField()
    cancelled = serializers.IntegerField()


__all__ = [
    "OrderCountsQuerySerializer",
    "OrderListQuerySerializer",
    "StatusCountsSerializer",
]
