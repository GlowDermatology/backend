from ipaddress import ip_network
from typing import Any

import pytest
from django.test import Client, override_settings


@override_settings(ADMIN_API_ALLOWED_NETS=[ip_network("10.0.0.0/8")])
def test_admin_gate_blocks_outside_peer() -> None:
    response = Client().get("/api/v1/admin/test/", REMOTE_ADDR="8.8.8.8", HTTP_X_GD_CLIENT_IP="10.0.0.2")
    assert response.status_code == 403
    assert response.json()["code"] == "permission_denied"


@override_settings(
    ADMIN_API_ALLOWED_NETS=[ip_network("1.2.3.0/24")], TRUSTED_PROXIES=[ip_network("10.0.0.0/8")]
)
def test_admin_gate_uses_proxy_resolved_peer() -> None:
    response = Client().get("/api/v1/admin/test/", REMOTE_ADDR="10.0.0.9", HTTP_X_FORWARDED_FOR="1.2.3.4")
    assert response.status_code == 404


@override_settings(ADMIN_API_REQUIRE_INTERNAL_SECRET=True, INTERNAL_API_SECRET="x" * 32)
def test_admin_gate_requires_secret_when_enabled() -> None:
    client = Client()
    assert client.get("/api/v1/admin/test/").status_code == 403
    assert client.get("/api/v1/admin/test/", HTTP_X_GD_INTERNAL_SECRET="x" * 32).status_code == 404


@override_settings(ADMIN_API_ALLOWED_NETS=[])
@pytest.mark.parametrize("path", ["/api/v1/docs/", "/api/v1/schema/"])
def test_docs_gated(path: str) -> None:
    assert Client().get(path).status_code == 403


@override_settings(ADMIN_API_ALLOWED_NETS=[], ADMIN_API_REQUIRE_INTERNAL_SECRET=True)
def test_health_not_gated() -> None:
    assert Client().get("/api/v1/health/").status_code == 200


# --- the ops console (/django-admin/): OPS_ADMIN_ALLOWED_NETS against the edge-forwarded client address ----

OFFICE = [ip_network("203.0.113.0/24")]
EDGE = [ip_network("172.30.0.10/32")]


@override_settings(OPS_ADMIN_ALLOWED_NETS=OFFICE, TRUSTED_PROXIES=EDGE)
@pytest.mark.parametrize(
    ("remote", "forwarded", "status"),
    [
        ("172.30.0.10", "203.0.113.7", 200),  # through the edge, from the office
        ("172.30.0.10", "198.51.100.7", 404),  # through the edge, from elsewhere
        ("172.30.0.10", "", 404),  # the edge itself (no forwarded hop)
        ("198.51.100.7", "203.0.113.7", 404),  # a spoofed X-Forwarded-For from an untrusted peer
        ("172.30.0.20", "203.0.113.7", 404),  # the Next container is not a trusted proxy
        ("203.0.113.7", "", 200),  # directly from the office (no proxy)
        ("172.30.0.10", "198.51.100.7, 203.0.113.7", 200),  # the edge's own hop is the last one
        ("172.30.0.10", "203.0.113.7, 198.51.100.7", 404),  # a client-sent hop before it is ignored
    ],
)
def test_console_gate_uses_the_edge_forwarded_address(remote: str, forwarded: str, status: int) -> None:
    extra: dict[str, Any] = {"HTTP_X_FORWARDED_FOR": forwarded} if forwarded else {}
    response = Client().get("/django-admin/login/", REMOTE_ADDR=remote, **extra)
    assert response.status_code == status
    if status == 404:
        assert response.content == b""  # like the edge: nothing reveals the console


@override_settings(OPS_ADMIN_ALLOWED_NETS=OFFICE, TRUSTED_PROXIES=EDGE, INTERNAL_API_SECRET="x" * 32)
def test_console_gate_ignores_the_bff_client_ip_header() -> None:
    response = Client().get(
        "/django-admin/login/",
        REMOTE_ADDR="198.51.100.7",
        HTTP_X_GD_INTERNAL_SECRET="x" * 32,
        HTTP_X_GD_CLIENT_IP="203.0.113.7",
    )
    assert response.status_code == 404


@override_settings(OPS_ADMIN_ALLOWED_NETS=[])
@pytest.mark.parametrize(
    "path",
    ["/django-admin/", "/django-admin", "/django-admin/login/", "/django-admin/2fa/", "/django-adminx"],
)
def test_every_console_path_is_gated(path: str) -> None:
    response = Client().get(path)
    assert response.status_code == 404 and response.content == b""


@override_settings(OPS_ADMIN_ALLOWED_NETS=[], ADMIN_API_ALLOWED_NETS=[ip_network("0.0.0.0/0")])
def test_static_files_and_the_api_are_not_behind_the_console_gate() -> None:
    assert Client().get("/static/ops/signin.css").status_code in {200, 404}  # never the gate's empty 404
    assert Client().get("/api/v1/health/").status_code == 200
    # the staff API keeps its own rule (ADMIN_API_ALLOWED_NETS), not the console's
    assert Client().get("/api/v1/admin/test/").status_code == 404


@override_settings(OPS_ADMIN_ALLOWED_NETS=OFFICE, ADMIN_API_ALLOWED_NETS=[])
def test_the_console_nets_never_open_the_staff_api() -> None:
    response = Client().get("/api/v1/admin/test/", REMOTE_ADDR="203.0.113.7")
    assert response.status_code == 403
