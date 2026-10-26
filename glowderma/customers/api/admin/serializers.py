"""Admin serializers of `customers` (contract §2.6; display rules §5.3).

Every serializer reads the frozen rows of ``loyalty.selectors`` / ``customers.selectors`` (attributes named as
the contract keys); none touches another module's ORM row except the annotated ``Customer`` of the list.
``CustomerListItem`` is declared once, by ``notifications`` (the segment members list returns the same shape),
and the list view renders with it over rows annotated by ``loyalty.selectors.list_annotations()``.
"""

from __future__ import annotations

from typing import Any, cast

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from glowderma.core import enums
from glowderma.core.api.fields import PercentField
from glowderma.core.api.serializers import ActorSerializer
from glowderma.customers.selectors import DEFAULT_ORDERING, ORDERINGS, SEARCH_MAX_LENGTH
from glowderma.loyalty.selectors import LedgerRow
from glowderma.loyalty.services import ADJUSTMENT_MAX_AMOUNT, ADJUSTMENT_MIN_AMOUNT

REASON_MIN_LENGTH = 3
REASON_MAX_LENGTH = 300


class CustomerListQuerySerializer(serializers.Serializer[dict[str, Any]]):
    search = serializers.CharField(required=False, allow_blank=True, max_length=SEARCH_MAX_LENGTH)
    status = serializers.ChoiceField(choices=enums.CustomerStatus.values, required=False)
    segment = serializers.IntegerField(required=False, min_value=1)
    ordering = serializers.ChoiceField(choices=list(ORDERINGS), required=False, default=DEFAULT_ORDERING)


# --- card -------------------------------------------------------------------------------------------------


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
        help_text="`false` after a logout, a sign-in elsewhere or the deletion."
    )
    last_active_at = serializers.DateTimeField()
    created_at = serializers.DateTimeField(help_text="When the installation first signed in.")


class CustomerCardSerializer(serializers.Serializer[Any]):
    """Contract §2.6 ``CustomerCard``."""

    id = serializers.IntegerField()
    name = serializers.CharField(allow_blank=True)
    phone = serializers.CharField()
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


# --- adjustments ------------------------------------------------------------------------------------------


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
