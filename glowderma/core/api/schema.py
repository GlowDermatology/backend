from typing import Any

from rest_framework import serializers


class ErrorItemSerializer(serializers.Serializer[dict[str, Any]]):
    field = serializers.CharField(allow_null=True)
    code = serializers.CharField()
    message = serializers.CharField()


class ErrorEnvelopeSerializer(serializers.Serializer[dict[str, Any]]):
    code = serializers.CharField()
    message = serializers.CharField()
    errors = ErrorItemSerializer(many=True)  # type: ignore[assignment]
    request_id = serializers.CharField()
