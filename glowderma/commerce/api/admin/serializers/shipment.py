"""The shipment union of an admin order (contract §2.2 table)."""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import PolymorphicProxySerializer
from rest_framework import serializers

from glowderma.core import enums


class ShipmentNotRequiredSerializer(serializers.Serializer[Any]):
    state = serializers.CharField(help_text="`not_required` (pickup)")


class ShipmentNotCreatedSerializer(serializers.Serializer[Any]):
    state = serializers.CharField(help_text="`not_created`")
    pending = serializers.BooleanField()


class ShipmentFailedSerializer(serializers.Serializer[Any]):
    state = serializers.CharField(help_text="`failed`")
    last_attempt_at = serializers.DateTimeField()
    attempts = serializers.IntegerField()
    pending = serializers.BooleanField()


class ShipmentCreatedSerializer(serializers.Serializer[Any]):
    state = serializers.CharField(help_text="`created`")
    tracking_number = serializers.CharField()
    tariff = serializers.IntegerField()
    updated_at = serializers.DateTimeField()
    via = serializers.ChoiceField(choices=enums.ShipmentVia.values)


SHIPMENT_SERIALIZERS: dict[enums.ShipmentState, type[serializers.Serializer[Any]]] = {
    enums.ShipmentState.NOT_REQUIRED: ShipmentNotRequiredSerializer,
    enums.ShipmentState.NOT_CREATED: ShipmentNotCreatedSerializer,
    enums.ShipmentState.FAILED: ShipmentFailedSerializer,
    enums.ShipmentState.CREATED: ShipmentCreatedSerializer,
}


SHIPMENT_SCHEMA = PolymorphicProxySerializer(
    component_name="Shipment",
    serializers={str(state.value): cls for state, cls in SHIPMENT_SERIALIZERS.items()},
    resource_type_field_name="state",
)


def render_shipment(view: dict[str, Any]) -> dict[str, Any]:
    return dict(SHIPMENT_SERIALIZERS[enums.ShipmentState(view["state"])](view).data)


__all__ = [
    "SHIPMENT_SCHEMA",
    "SHIPMENT_SERIALIZERS",
    "ShipmentCreatedSerializer",
    "ShipmentFailedSerializer",
    "ShipmentNotCreatedSerializer",
    "ShipmentNotRequiredSerializer",
    "render_shipment",
]
