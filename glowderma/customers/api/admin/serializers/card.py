"""The admin customer card (admin contract §2.6): ledger, purchases, totals, tier, groups and devices."""

from __future__ import annotations

from typing import Any, cast

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from glowderma.core import enums
from glowderma.core.api.fields import PercentField
from glowderma.core.api.serializers import ActorSerializer
from glowderma.loyalty.selectors import LedgerRow


class LedgerEntrySerializer(serializers.Serializer[LedgerRow]):
    """Contract §2.6 ``LedgerEntry``."""

    id = serializers.IntegerField()
    at = serializers.DateTimeField()
    kind = serializers.ChoiceField(choices=enums.LedgerKind.values)
    amount = serializers.IntegerField()
    balance_after = serializers.IntegerField()
    source_type = serializers.ChoiceField(choices=enums.LedgerSourceType.values)
    source_ref = serializers.SerializerMethodField()
    order_id = serializers.IntegerField(allow_null=True)
    base = serializers.IntegerField(allow_null=True)
    percent = PercentField(allow_null=True)
    promo_excluded = serializers.BooleanField()
    policy_version = serializers.IntegerField(allow_null=True)
    reverses_id = serializers.IntegerField(allow_null=True)
    reason = serializers.CharField(allow_null=True)
    actor = serializers.SerializerMethodField()

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_source_ref(self, obj: LedgerRow) -> str | None:
        """order → the order number as a string; billz_sale → the Billz document no.; admin → ``null``."""
        if obj.source_type == enums.LedgerSourceType.ADMIN:
            return None
        if obj.source_type == enums.LedgerSourceType.ORDER and obj.order_id is not None:
            return str(obj.order_id)
        return obj.source_ref

    @extend_schema_field(ActorSerializer(allow_null=True))
    def get_actor(self, obj: LedgerRow) -> dict[str, Any] | None:
        actor = obj.actor
        return None if actor is None else {"id": actor.id, "display_name": actor.display_name}


class PurchaseSerializer(serializers.Serializer[Any]):
    """Contract §2.6 ``Purchase``."""

    id = serializers.IntegerField()
    at = serializers.DateTimeField()
    source = serializers.ChoiceField(choices=enums.PurchaseSource.values)  # type: ignore[assignment]  # a field, not Field.source
    order_id = serializers.IntegerField(allow_null=True)
    billz_sale_ref = serializers.CharField(allow_null=True)
    amount = serializers.IntegerField()
    promo_amount = serializers.IntegerField()
    cashback = serializers.IntegerField()
    policy_version = serializers.IntegerField(allow_null=True)
    reversed = serializers.BooleanField()


class CustomerTotalsSerializer(serializers.Serializer[Any]):
    store = serializers.IntegerField()
    online = serializers.IntegerField()
    lifetime = serializers.IntegerField()


class OnlineOrdersSerializer(serializers.Serializer[Any]):
    count = serializers.IntegerField(help_text="Paid, non-reversed online purchases.")
    last_order_id = serializers.IntegerField(allow_null=True, help_text="The newest order of any status.")
    last_order_at = serializers.DateTimeField(allow_null=True)
    all_count = serializers.IntegerField(
        help_text="Every order of any status: what `orders/?customer={id}` lists (the card's orders link)."
    )


NextTierSerializer = cast(
    type[serializers.Serializer[Any]],
    type(
        "NextTierSerializer",
        (serializers.Serializer,),
        {
            "__module__": __name__,
            "__doc__": "The next band: its percent, start, the so'm left and the 0..1 progress.",
            "percent": PercentField(),
            "from": serializers.IntegerField(source="from_amount"),
            "remaining": serializers.IntegerField(),
            "progress": serializers.FloatField(),
        },
    ),
)


class CustomerTierSerializer(serializers.Serializer[Any]):
    policy_version = serializers.IntegerField()
    percent = PercentField()
    next = NextTierSerializer(allow_null=True)


class CustomerGroupSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    name = serializers.CharField()


class CustomerDeviceSerializer(serializers.Serializer[Any]):
    """The card's current ``device``: the most recently active of the customer's active devices."""

    platform = serializers.ChoiceField(choices=enums.OrderChannel.values)
    model = serializers.CharField(allow_blank=True)
    os = serializers.CharField(allow_blank=True)
    app_version = serializers.CharField(allow_blank=True)
    last_active_at = serializers.DateTimeField()
    push_enabled = serializers.BooleanField()


class CustomerDeviceRowSerializer(serializers.Serializer[Any]):
    """One of the card's ``devices`` (gap ADM-8.1): every installation, active ones first; never the FCM
    token, the device key or the Firebase installation id."""

    id = serializers.IntegerField()
    platform = serializers.ChoiceField(choices=enums.OrderChannel.values)
    model = serializers.CharField(allow_blank=True)
    os = serializers.CharField(allow_blank=True)
    app_version = serializers.CharField(allow_blank=True)
    push_enabled = serializers.BooleanField()
    is_active = serializers.BooleanField(
        help_text="`false` once no session is left on it (a logout, a session signed out, a password reset "
        "or change elsewhere) or after the deletion (B-W1: several devices may be active)."
    )
    last_active_at = serializers.DateTimeField()
    created_at = serializers.DateTimeField(help_text="When the installation first signed in.")


class CustomerCardSerializer(serializers.Serializer[Any]):
    """Contract §2.6 ``CustomerCard``."""

    id = serializers.IntegerField()
    name = serializers.CharField(allow_blank=True)
    phone = serializers.CharField(allow_null=True, help_text="Null for an account without a phone yet.")
    email = serializers.CharField(allow_null=True, help_text="Verified e-mail address | null.")
    status = serializers.ChoiceField(choices=enums.CustomerStatus.values)
    registered_at = serializers.DateTimeField()
    deleted_at = serializers.DateTimeField(allow_null=True)
    language = serializers.ChoiceField(choices=enums.ContentLanguage.values)
    address = serializers.CharField(allow_null=True)
    totals = CustomerTotalsSerializer()
    online_orders = OnlineOrdersSerializer()
    tier = CustomerTierSerializer(allow_null=True)
    balance = serializers.IntegerField()
    held = serializers.IntegerField()
    available = serializers.IntegerField()
    groups = CustomerGroupSerializer(many=True)
    upcoming_appointments = serializers.IntegerField()
    device = CustomerDeviceSerializer(allow_null=True)
    devices = CustomerDeviceRowSerializer(many=True)
    ledger = LedgerEntrySerializer(many=True)
    purchases = PurchaseSerializer(many=True)


__all__ = [
    "CustomerCardSerializer",
    "CustomerDeviceRowSerializer",
    "CustomerDeviceSerializer",
    "CustomerGroupSerializer",
    "CustomerTierSerializer",
    "CustomerTotalsSerializer",
    "LedgerEntrySerializer",
    "NextTierSerializer",
    "OnlineOrdersSerializer",
    "PurchaseSerializer",
]
