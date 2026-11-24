"""The delivery object of an admin order (Yandex Delivery plan §3.10)."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from glowderma.core import enums


class DeliveryIssueRefSerializer(serializers.Serializer[Any]):
    """One open issue on the order card (the queue has the full row: ``GET delivery/issues/``)."""

    id = serializers.IntegerField()
    kind = serializers.ChoiceField(choices=enums.DeliveryIssueKind.values)
    code = serializers.CharField()
    urgent = serializers.BooleanField()
    opened_at = serializers.DateTimeField()


class OrderDeliverySerializer(serializers.Serializer[Any]):
    """The order's carrier view (``null`` for pickup). BTS: ``provider`` and ``open_issues`` only, every
    Yandex key ``null``. Amounts in so'm; ``claim_status`` is Yandex's status verbatim; no courier phone."""

    provider = serializers.ChoiceField(choices=enums.DeliveryProvider.values)
    method = serializers.ChoiceField(choices=enums.DeliveryMethod.values, allow_null=True)
    stage = serializers.ChoiceField(choices=enums.ShipmentStage.values, allow_null=True)
    promised_from = serializers.DateTimeField(allow_null=True)
    promised_to = serializers.DateTimeField(allow_null=True)
    quote_fee = serializers.IntegerField(allow_null=True, help_text="The fee the checkout quote charged.")
    carrier_price = serializers.IntegerField(
        allow_null=True, help_text="Yandex's estimate the checkout quote was computed from."
    )
    offer_price = serializers.IntegerField(
        allow_null=True, help_text="The binding offer of the current courier claim (VAT included)."
    )
    approved_price = serializers.IntegerField(allow_null=True)
    carrier_cost_total = serializers.IntegerField(
        allow_null=True, help_text="Yandex's final prices of every claim of the shipment, summed."
    )
    cost_difference = serializers.IntegerField(allow_null=True)
    parcel_weight_g = serializers.IntegerField(allow_null=True)
    prepared_at = serializers.DateTimeField(allow_null=True)
    courier_name = serializers.CharField(allow_null=True)
    courier_vehicle = serializers.CharField(allow_null=True)
    tracking_url = serializers.CharField(allow_null=True)
    claim_status = serializers.CharField(allow_null=True)
    claim_number = serializers.IntegerField(allow_null=True)
    failure_code = serializers.CharField(allow_null=True)
    open_issues = DeliveryIssueRefSerializer(many=True)


def render_delivery(view: dict[str, Any] | None) -> dict[str, Any] | None:
    return None if view is None else dict(OrderDeliverySerializer(view).data)


__all__ = [
    "DeliveryIssueRefSerializer",
    "OrderDeliverySerializer",
    "render_delivery",
]
