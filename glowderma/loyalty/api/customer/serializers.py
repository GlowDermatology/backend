"""Response shapes of the customer loyalty routes (customer contract §2.9) and the POS resolve (§2.10)."""

from __future__ import annotations

from typing import Any, cast

from rest_framework import serializers

from glowderma.core import enums
from glowderma.core.api.fields import PercentField

CashbackBandSerializer = cast(
    type[serializers.Serializer[Any]],
    type(
        "CashbackBandSerializer",
        (serializers.Serializer,),
        {
            "__module__": __name__,
            "__doc__": "One band of the active policy: ``from`` inclusive, ``to`` exclusive | null.",
            "from": serializers.IntegerField(source="from_amount"),
            "to": serializers.IntegerField(source="to_amount", allow_null=True),
            "percent": PercentField(),
        },
    ),
)

CashbackNextTierSerializer = cast(
    type[serializers.Serializer[Any]],
    type(
        "CashbackNextTierSerializer",
        (serializers.Serializer,),
        {
            "__module__": __name__,
            "__doc__": "The band above the current one: what is left to reach it and the progress (0..1).",
            "percent": PercentField(),
            "from": serializers.IntegerField(source="from_amount"),
            "remaining": serializers.IntegerField(),
            "progress": serializers.FloatField(),
        },
    ),
)


class CashbackHoldItemSerializer(serializers.Serializer[Any]):
    order_id = serializers.IntegerField()
    amount = serializers.IntegerField()
    expires_at = serializers.DateTimeField(
        help_text="Effective expiry: the reservation end, plus the in-flight grace while a transaction "
        "attached in time is still open."
    )


class CashbackSummarySerializer(serializers.Serializer[Any]):
    balance = serializers.IntegerField()
    held = serializers.IntegerField()
    available = serializers.IntegerField(help_text="balance − active holds; may be negative (L5).")
    negative = serializers.BooleanField()
    lifetime_total = serializers.IntegerField()
    tier = CashbackBandSerializer(allow_null=True)
    next_tier = CashbackNextTierSerializer(allow_null=True)
    tiers = CashbackBandSerializer(many=True)
    earn_on_cashback_paid = serializers.BooleanField()
    holds = CashbackHoldItemSerializer(many=True)
    qr_ttl_seconds = serializers.IntegerField()


class LedgerItemSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    at = serializers.DateTimeField()
    kind = serializers.ChoiceField(choices=enums.LedgerKind.values)
    amount = serializers.IntegerField()
    balance_after = serializers.IntegerField()
    title = serializers.CharField()
    subtitle = serializers.CharField(allow_null=True)
    order_id = serializers.IntegerField(allow_null=True)
    # A field named like ``Field.source``; DRF reads declared fields, not the attribute.
    source = serializers.ChoiceField(choices=enums.LedgerSource.values)  # type: ignore[assignment]


class QrTokenSerializer(serializers.Serializer[Any]):
    token = serializers.CharField(help_text='"GD1:" + base64url(16 random bytes); single use.')
    expires_at = serializers.DateTimeField()
    ttl_seconds = serializers.IntegerField()
    refresh_in_seconds = serializers.IntegerField()


class ResolveQrSerializer(serializers.Serializer[dict[str, Any]]):
    token = serializers.CharField(min_length=1, max_length=64, trim_whitespace=False)
    sale_ref = serializers.CharField(max_length=64, allow_null=True, allow_blank=True, required=False)


class ResolvedQrSerializer(serializers.Serializer[Any]):
    customer_ref = serializers.CharField()
    phone = serializers.CharField()
    phone_mask = serializers.CharField()
    available = serializers.IntegerField()
    percent = PercentField(allow_null=True)
    resolved_at = serializers.DateTimeField()
