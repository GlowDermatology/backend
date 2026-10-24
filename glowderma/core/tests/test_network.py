import pytest
from django.test import Client, override_settings


@override_settings(ADMIN_API_ALLOWED_NETS=["10.0.0.0/8"])
def test_admin_gate_blocks_outside_peer() -> None:
    response = Client().get("/api/v1/admin/test/", REMOTE_ADDR="8.8.8.8", HTTP_X_GD_CLIENT_IP="10.0.0.2")
    assert response.status_code == 403
    assert response.json()["code"] == "permission_denied"


@override_settings(ADMIN_API_ALLOWED_NETS=["1.2.3.0/24"], TRUSTED_PROXIES=["10.0.0.0/8"])
def test_admin_gate_uses_proxy_resolved_peer() -> None:
    response = Client().get("/api/v1/admin/test/", REMOTE_ADDR="10.0.0.9", HTTP_X_FORWARDED_FOR="1.2.3.4")
    assert response.status_code == 404


@override_settings(ADMIN_API_REQUIRE_INTERNAL_SECRET=True, INTERNAL_API_SECRET="x" * 32)
def test_admin_gate_requires_secret_when_enabled() -> None:
    client = Client()
    assert client.get("/api/v1/admin/test/").status_code == 403
    assert client.get("/api/v1/admin/test/", HTTP_X_GD_INTERNAL_SECRET="x" * 32).status_code == 404


@override_settings(ADMIN_API_ALLOWED_NETS=[])
@pytest.mark.parametrize("path", ["/api/v1/docs/", "/api/v1/schema/", "/django-admin/"])
def test_docs_gated(path: str) -> None:
    assert Client().get(path).status_code == 403


@override_settings(ADMIN_API_ALLOWED_NETS=[], ADMIN_API_REQUIRE_INTERNAL_SECRET=True)
def test_health_not_gated() -> None:
    assert Client().get("/api/v1/health/").status_code == 200
