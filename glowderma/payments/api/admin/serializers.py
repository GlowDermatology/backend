"""Admin serializers of `payments` (contract §2.12 ``GET payments/``). The ``Refund`` and the period query
shapes are shared with the orders screens of ``commerce`` (above this module); ``CustomerRef`` lives in
``core.api.serializers``."""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from glowderma.core import enums
from glowderma.core.api.exceptions import field_error
from glowderma.core.api.serializers import CustomerRefSerializer
from glowderma.payments.selectors import RefundRow

SEARCH_MAX_LENGTH = 64


class RefundSerializer(serializers.Serializer[RefundRow]):
    """Contract ``Refund`` (§2.2): ``completed_at`` is set on ``succeeded`` only."""

    id = serializers.IntegerField()
    state = serializers.ChoiceField(choices=enums.RefundState.values)
    provider = serializers.ChoiceField(choices=enums.PaymentProvider.values)
    amount = serializers.IntegerField()
    requested_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()
    completed_at = serializers.DateTimeField(allow_null=True)


class PaymentSerializer(serializers.Serializer[Any]):
    """One ``GET payments/`` row; ``refunds`` come from the view's per-page lookup (newest first)."""

    id = serializers.IntegerField(source="pk")
    order_id = serializers.IntegerField()
    customer = CustomerRefSerializer(source="order.customer")
    provider = serializers.ChoiceField(choices=enums.PaymentProvider.values)
    status = serializers.ChoiceField(choices=enums.PaymentStatus.values)
    amount = serializers.IntegerField()
    transaction_id = serializers.SerializerMethodField()
    created_at = serializers.DateTimeField()
    confirmed_at = serializers.DateTimeField(allow_null=True)
    refunds = serializers.SerializerMethodField()

    def get_transaction_id(self, obj: Any) -> str | None:
        return obj.provider_txn_id or None

    @extend_schema_field(RefundSerializer(many=True))
    def get_refunds(self, obj: Any) -> Any:
        rows = self.context.get("refunds", {}).get(obj.pk, [])
        return RefundSerializer(rows, many=True).data


class PeriodQuerySerializer(serializers.Serializer[dict[str, Any]]):
    """``period`` / ``date_from`` / ``date_to`` of ``orders/`` and ``payments/`` (contract §2.2): inclusive
    Tashkent days ANDed with the period; ``date_from > date_to`` → 400 on ``date_from``."""

    period = serializers.ChoiceField(
        choices=enums.OrderPeriod.values, required=False, default=enums.OrderPeriod.ALL
    )
    date_from = serializers.DateField(required=False)
    date_to = serializers.DateField(required=False)
    search = serializers.CharField(required=False, max_length=SEARCH_MAX_LENGTH, allow_blank=True)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        date_from, date_to = attrs.get("date_from"), attrs.get("date_to")
        if date_from is not None and date_to is not None and date_from > date_to:
            raise serializers.ValidationError({"date_from": [field_error("invalid")]})
        if not attrs.get("search"):
            attrs.pop("search", None)
        return attrs


class PaymentListQuerySerializer(PeriodQuerySerializer):
    status = serializers.ChoiceField(choices=enums.PaymentStatus.values, required=False)
    provider = serializers.ChoiceField(choices=enums.PaymentProvider.values, required=False)
    refund_state = serializers.ChoiceField(choices=enums.RefundState.values, required=False)
    ordering = serializers.ChoiceField(
        choices=["-created_at", "created_at"], required=False, default="-created_at"
    )
