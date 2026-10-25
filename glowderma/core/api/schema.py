from typing import Any

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter
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


# Contract 1.7: "`Idempotency-Key` ... UUID string (36 chars)", required on the endpoints marked I.
IDEMPOTENCY_KEY_HEADER = OpenApiParameter(
    name="Idempotency-Key",
    type=OpenApiTypes.UUID,
    location=OpenApiParameter.HEADER,
    required=True,
    description="Client-generated UUID; replays return the stored response with `Idempotent-Replayed: true`.",
)
# Contract 1.8: "?export=csv|xlsx" on every list marked E.
EXPORT_PARAM = OpenApiParameter(
    name="export",
    type=OpenApiTypes.STR,
    location=OpenApiParameter.QUERY,
    required=False,
    enum=["csv", "xlsx"],
    description="Download the filtered list as a file instead of the JSON page.",
)
# Error responses every admin endpoint can produce (contract 1.6).
ADMIN_ERRORS: dict[int, type[serializers.Serializer[Any]]] = dict.fromkeys(
    (400, 401, 403, 404, 409, 422, 429), ErrorEnvelopeSerializer
)
# Unauthenticated endpoints (login, refresh, OAuth) never produce the authorization or state errors.
PUBLIC_ERRORS: dict[int, type[serializers.Serializer[Any]]] = dict.fromkeys(
    (400, 401, 429), ErrorEnvelopeSerializer
)
