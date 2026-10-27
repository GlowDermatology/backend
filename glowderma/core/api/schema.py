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
# Customer contract 1.7: optional on every other customer write, honoured when sent
# (``@idempotent(required=False)``).
OPTIONAL_IDEMPOTENCY_KEY_HEADER = OpenApiParameter(
    name="Idempotency-Key",
    type=OpenApiTypes.UUID,
    location=OpenApiParameter.HEADER,
    required=False,
    description=(
        "Optional client-generated UUID; when sent, a retry with the same body returns the stored 2xx "
        "response with `Idempotent-Replayed: true` and another body answers 422 `idempotency_key_reused`."
    ),
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


# --- Customer API (customer contract §1.2, §1.6) ---


class CustomerErrorEnvelopeSerializer(serializers.Serializer[dict[str, Any]]):
    """The customer envelope: the admin keys plus ``details`` (always present, ``null`` by default)."""

    code = serializers.CharField()
    message = serializers.CharField(help_text="Localized (Accept-Language → the customer's language → uz).")
    errors = ErrorItemSerializer(many=True)  # type: ignore[assignment]
    details = serializers.JSONField(allow_null=True, help_text="Code-specific object (§1.6) or null.")
    request_id = serializers.CharField()


# Every signed-in (C) customer route; the auth routes use it too (409 phone rules, 503 SMS budget).
CUSTOMER_ERRORS: dict[int, type[serializers.Serializer[Any]]] = dict.fromkeys(
    (400, 401, 404, 409, 422, 426, 429, 503), CustomerErrorEnvelopeSerializer
)
# Optional-auth (O) read routes: catalog, settings, regions, specialists.
PUBLIC_CUSTOMER_ERRORS: dict[int, type[serializers.Serializer[Any]]] = dict.fromkeys(
    (400, 401, 404, 426, 429), CustomerErrorEnvelopeSerializer
)


def device_headers(*, required: bool = True) -> list[OpenApiParameter]:
    """``X-GD-Device-Id``, ``X-GD-Platform`` and ``X-GD-App-Version`` (§1.2): required on auth and C routes,
    optional on O routes."""
    return [
        OpenApiParameter(
            name="X-GD-Device-Id",
            type=OpenApiTypes.STR,
            location=OpenApiParameter.HEADER,
            required=required,
            pattern=r"^[A-Za-z0-9_-]{8,128}$",
            description="Firebase installation id of this app install (an identifier, not a secret).",
        ),
        OpenApiParameter(
            name="X-GD-Platform",
            type=OpenApiTypes.STR,
            location=OpenApiParameter.HEADER,
            required=required,
            enum=["ios", "android", "web"],
            description="Device platform (`OrderChannel` literals); `web` = the website's server-side BFF.",
        ),
        OpenApiParameter(
            name="X-GD-App-Version",
            type=OpenApiTypes.STR,
            location=OpenApiParameter.HEADER,
            # required with ios/android wherever the device id is; optional (and never floored) for web
            required=False,
            pattern=r"^\d{1,3}\.\d{1,3}\.\d{1,3}$",
            description=(
                "App version, required with `ios`/`android` (below the platform's minimum → 426 "
                "`app_update_required`); optional for `web`, where no minimum applies."
            ),
        ),
    ]


DEVICE_HEADERS: list[OpenApiParameter] = device_headers(required=True)
OPTIONAL_DEVICE_HEADERS: list[OpenApiParameter] = device_headers(required=False)
DEVICE_KEY_HEADER = OpenApiParameter(
    name="X-GD-Device-Key",
    type=OpenApiTypes.STR,
    location=OpenApiParameter.HEADER,
    required=False,
    pattern=r"^[A-Za-z0-9_-]{43}$",
    description="The device key issued once in `AuthTokens.device_key`; proves a known device (§1.9).",
)
ACCEPT_LANGUAGE_HEADER = OpenApiParameter(
    name="Accept-Language",
    type=OpenApiTypes.STR,
    location=OpenApiParameter.HEADER,
    required=False,
    description="First supported tag among uz, ru, en in q-order; else the customer's language, else uz.",
)
