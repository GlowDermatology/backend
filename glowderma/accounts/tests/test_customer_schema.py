"""OpenAPI with the customer audience (plan §1 P0 "Schema"): every new enum gets its own component, the
customer views carry the customer security scheme and error envelope, and drf-spectacular warns about
nothing."""

from types import ModuleType
from typing import Any

import pytest
from django.test import override_settings
from django.urls import clear_url_caches, path
from drf_spectacular.drainage import GENERATOR_STATS
from drf_spectacular.generators import SchemaGenerator
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.customer_base import CustomerAPIView, PublicAPIView
from glowderma.core import enums
from glowderma.core.api.schema import (
    ACCEPT_LANGUAGE_HEADER,
    CUSTOMER_ERRORS,
    DEVICE_HEADERS,
    DEVICE_KEY_HEADER,
    IDEMPOTENCY_KEY_HEADER,
    OPTIONAL_DEVICE_HEADERS,
    PUBLIC_CUSTOMER_ERRORS,
)

NEW_ENUMS = [enum for enum in enums.CONTRACT_ENUMS if enum.__name__ in {
    "OtpPurpose", "NextStep", "CustomerSocialProvider", "SocialTokenType", "StockStatus",
    "CustomerProductOrdering", "CartLineStatus", "OrderScope", "CustomerRefundState", "CashbackEarnedState",
    "TimelineType", "ProgressStep", "ProgressState", "LedgerSource", "AppointmentScope",
    "AppointmentCancelledBy", "InboxKind", "InboxLinkKind", "PushDataType", "AccountSecurityEvent",
}]  # fmt: skip


def enum_field_name(enum: type) -> str:
    return "".join(f"_{char.lower()}" if char.isupper() else char for char in enum.__name__).lstrip("_")


EveryEnumSerializer: Any = type(
    "EveryEnumSerializer",
    (serializers.Serializer,),
    {
        **{enum_field_name(enum): serializers.ChoiceField(choices=enum.values) for enum in NEW_ENUMS},
        "platform": serializers.ChoiceField(choices=enums.OrderChannel.values),
    },
)


class SampleCustomerView(CustomerAPIView):
    @extend_schema(
        operation_id="customer_sample_create",
        request=EveryEnumSerializer,
        responses={200: EveryEnumSerializer, **CUSTOMER_ERRORS},
        parameters=[*DEVICE_HEADERS, DEVICE_KEY_HEADER, ACCEPT_LANGUAGE_HEADER, IDEMPOTENCY_KEY_HEADER],
    )
    def post(self, request: Request) -> Response:  # pragma: no cover - schema only
        return Response({})


class SamplePublicView(PublicAPIView):
    @extend_schema(
        operation_id="customer_sample_list",
        responses={200: EveryEnumSerializer, **PUBLIC_CUSTOMER_ERRORS},
        parameters=[*OPTIONAL_DEVICE_HEADERS, ACCEPT_LANGUAGE_HEADER],
    )
    def get(self, request: Request) -> Response:  # pragma: no cover - schema only
        return Response({})


def generate(urlpatterns: list[Any]) -> dict[str, Any]:
    module: Any = ModuleType("customer_schema_urls")
    module.urlpatterns = urlpatterns
    GENERATOR_STATS.reset()
    try:
        with override_settings(ROOT_URLCONF=module):
            clear_url_caches()
            schema = SchemaGenerator().get_schema(request=None, public=True)  # type: ignore[no-untyped-call]
    finally:
        clear_url_caches()
    return dict(schema)


def test_customer_views_and_every_new_enum_generate_without_warnings() -> None:
    schema = generate(
        [
            path("api/v1/sample/", SampleCustomerView.as_view()),
            path("api/v1/sample-public/", SamplePublicView.as_view()),
        ]
    )
    assert not GENERATOR_STATS, "drf-spectacular reported warnings or errors"
    components = schema["components"]["schemas"]
    for enum in NEW_ENUMS:
        assert components[f"{enum.__name__}Enum"]["enum"] == list(enum.values), enum.__name__
    assert components["OrderChannelEnum"]["enum"] == ["ios", "android"]
    assert "DevicePlatformEnum" not in components
    assert set(components["CustomerErrorEnvelope"]["properties"]) == {
        "code",
        "message",
        "errors",
        "details",
        "request_id",
    }
    assert schema["components"]["securitySchemes"]["CustomerJWT"] == {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "JWT",
    }
    post = schema["paths"]["/api/v1/sample/"]["post"]
    assert post["security"] == [{"CustomerJWT": []}]
    assert {"X-GD-Device-Id", "X-GD-Platform", "X-GD-App-Version", "X-GD-Device-Key", "Idempotency-Key"} <= {
        parameter["name"] for parameter in post["parameters"]
    }
    assert {"426", "503"} <= set(post["responses"])
    public = schema["paths"]["/api/v1/sample-public/"]["get"]
    assert {"CustomerJWT": []} in public["security"] and {} in public["security"]


@pytest.mark.django_db
def test_staff_routes_keep_their_provider_enum_without_apple() -> None:
    schema = SchemaGenerator().get_schema(request=None, public=True)  # type: ignore[no-untyped-call]
    components = schema["components"]["schemas"]
    assert components["SocialProviderEnum"]["enum"] == ["google", "facebook"]
    assert "apple" not in str(schema["paths"]["/api/v1/admin/auth/oauth/{provider}/"])
