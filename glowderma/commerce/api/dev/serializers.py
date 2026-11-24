"""The shapes of the payment simulator (local and test settings only): the request, a step and the result."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from glowderma.core import enums

OUTCOMES = ("success", "fail")


class SimulateSerializer(serializers.Serializer[dict[str, Any]]):
    outcome = serializers.ChoiceField(choices=list(OUTCOMES))
    provider = serializers.ChoiceField(
        choices=enums.PaymentProvider.values, required=False, allow_null=True, default=None
    )


class SimulateStepSerializer(serializers.Serializer[dict[str, Any]]):
    call = serializers.CharField()
    response = serializers.JSONField()


class SimulateResultSerializer(serializers.Serializer[dict[str, Any]]):
    order_id = serializers.IntegerField()
    provider = serializers.ChoiceField(choices=enums.PaymentProvider.values)
    steps = SimulateStepSerializer(many=True)
