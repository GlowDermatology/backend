import importlib
from pathlib import Path

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
