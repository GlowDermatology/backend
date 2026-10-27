import importlib
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.urls import clear_url_caches
from drf_spectacular.generators import SchemaGenerator

import config.urls


def test_committed_schema_is_current() -> None:
    generated = SchemaGenerator().get_schema(request=None, public=True)  # type: ignore[no-untyped-call]
    committed = yaml.safe_load((Path(settings.BASE_DIR) / "openapi/schema.yaml").read_text())
    assert generated == committed


def test_one_customer_ref_component_serves_every_module() -> None:
    """Contract §2.0 ``CustomerRef``: one OpenAPI component (appointments, orders, payments)."""
    generated = SchemaGenerator().get_schema(request=None, public=True)  # type: ignore[no-untyped-call]
    components = generated["components"]["schemas"]
    assert "OrderCustomerRef" not in components
    assert set(components["CustomerRef"]["properties"]) == {"id", "name", "phone", "email"}
    assert sorted(components["CustomerRef"]["required"]) == ["email", "id", "name", "phone"]
    for component in ("AppointmentItem", "OrderSummary", "OrderDetail", "Payment"):
        assert components[component]["properties"]["customer"] == {"$ref": "#/components/schemas/CustomerRef"}


@pytest.mark.django_db
def test_docs_require_staff_outside_debug() -> None:
    user = get_user_model().objects.create_user("admin@x.uz", "Saffron!Harbor-Long72")
    client = Client()
    login = client.post(
        "/api/v1/admin/auth/login/",
        {"email": user.email, "password": "Saffron!Harbor-Long72"},
        content_type="application/json",
    )
    token = login.json()["access"]
    production_docs = {
        **settings.SPECTACULAR_SETTINGS,
        "SERVE_AUTHENTICATION": ["glowderma.accounts.authentication.StaffJWTAuthentication"],
        "SERVE_PERMISSIONS": ["glowderma.accounts.permissions.IsActiveStaff"],
    }
    try:
        with override_settings(DEBUG=False, SPECTACULAR_SETTINGS=production_docs):
            importlib.reload(config.urls)
            clear_url_caches()
            for path in ("/api/v1/schema/", "/api/v1/docs/"):
                assert client.get(path).status_code == 401
                assert client.get(path, HTTP_AUTHORIZATION=f"Bearer {token}").status_code == 200
                with override_settings(ADMIN_API_ALLOWED_NETS=[]):
                    assert client.get(path, HTTP_AUTHORIZATION=f"Bearer {token}").status_code == 403
    finally:
        importlib.reload(config.urls)
        clear_url_caches()


def test_schema_declares_error_envelope_and_staff_jwt() -> None:
    schema = SchemaGenerator().get_schema(request=None, public=True)  # type: ignore[no-untyped-call]
    assert schema is not None
    components = schema["components"]
    assert set(components["schemas"]["ErrorEnvelope"]["properties"]) == {
        "code",
        "message",
        "errors",
        "request_id",
    }
    assert components["securitySchemes"]["StaffJWT"] == {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "JWT",
    }
    assert settings.SPECTACULAR_SETTINGS["COMPONENT_SPLIT_REQUEST"] is True
    assert "/api/v1/schema/" not in schema["paths"]


def test_local_docs_use_sidecar() -> None:
    response = Client().get("/api/v1/docs/")
    assert response.status_code == 200
    assert b"drf_spectacular_sidecar" in response.content


def test_contract_enum_components_are_named_after_the_enum() -> None:
    schema = SchemaGenerator().get_schema(request=None, public=True)  # type: ignore[no-untyped-call]
    assert schema is not None
    schemas = schema["components"]["schemas"]
    assert schemas["LogoutScopeEnum"]["enum"] == ["current", "others", "all"]
    assert schemas["HealthStatusEnum"]["enum"] == ["ok", "error"]
    assert schemas["SocialProviderEnum"]["enum"] == ["google", "facebook"]
    assert not {"ScopeEnum", "StatusEnum", "LinkedProvidersEnum"} & set(schemas)
    overrides = cast(dict[str, Any], settings.SPECTACULAR_SETTINGS["ENUM_NAME_OVERRIDES"])
    assert overrides["OrderStatusEnum"] == "glowderma.core.enums.OrderStatus.values"


def test_schema_helpers_match_the_contract() -> None:
    """Contract 1.7: `Idempotency-Key` is a UUID header; 1.8: `?export=csv|xlsx`; 1.6: error envelope."""
    from drf_spectacular.utils import OpenApiParameter

    from glowderma.core.api.schema import (
        ADMIN_ERRORS,
        EXPORT_PARAM,
        IDEMPOTENCY_KEY_HEADER,
        PUBLIC_ERRORS,
        ErrorEnvelopeSerializer,
    )

    assert IDEMPOTENCY_KEY_HEADER.name == "Idempotency-Key"
    assert IDEMPOTENCY_KEY_HEADER.location == OpenApiParameter.HEADER
    assert IDEMPOTENCY_KEY_HEADER.required is True
    assert EXPORT_PARAM.name == "export"
    assert EXPORT_PARAM.location == OpenApiParameter.QUERY
    assert EXPORT_PARAM.enum == ["csv", "xlsx"]
    assert set(ADMIN_ERRORS) == {400, 401, 403, 404, 409, 422, 429}
    assert set(PUBLIC_ERRORS) == {400, 401, 429}
    assert set(ADMIN_ERRORS.values()) == set(PUBLIC_ERRORS.values()) == {ErrorEnvelopeSerializer}
