from ipaddress import ip_network
from unittest.mock import patch

from django.test import Client, RequestFactory, override_settings

from glowderma.core.clientip import client_ip, peer_ip


def test_client_ip_trusts_header_only_with_secret() -> None:
    request = RequestFactory().get("/", REMOTE_ADDR="8.8.8.8", HTTP_X_GD_CLIENT_IP="1.2.3.4")
    assert client_ip(request) == "8.8.8.8"
    with override_settings(INTERNAL_API_SECRET="x" * 32):
        request.META["HTTP_X_GD_INTERNAL_SECRET"] = "x" * 32
        assert client_ip(request) == "1.2.3.4"
        request.META["HTTP_X_GD_CLIENT_IP"] = "invalid"
        assert client_ip(request) == "8.8.8.8"


def test_client_ip_ignores_xff_from_untrusted_peer() -> None:
    request = RequestFactory().get("/", REMOTE_ADDR="8.8.8.8", HTTP_X_FORWARDED_FOR="1.2.3.4")
    assert client_ip(request) == "8.8.8.8"


@override_settings(TRUSTED_PROXIES=[ip_network("10.0.0.0/8")])
def test_client_ip_via_trusted_proxy() -> None:
    request = RequestFactory().get("/", REMOTE_ADDR="10.0.0.9", HTTP_X_FORWARDED_FOR="1.2.3.4, 10.0.0.5")
    assert client_ip(request) == "1.2.3.4"
    request.META["HTTP_X_FORWARDED_FOR"] = "1.2.3.4, invalid, 10.0.0.5"
    assert peer_ip(request) == "10.0.0.5"
    request.META["HTTP_X_FORWARDED_FOR"] = "1.2.3.4, , 10.0.0.5"
    assert peer_ip(request) == "10.0.0.5"


@override_settings(TRUSTED_PROXIES=[ip_network("127.0.0.0/8")])
def test_requests_do_not_reparse_networks() -> None:
    with patch("ipaddress.ip_network", side_effect=AssertionError("network parsing during request")):
        assert Client().get("/api/v1/health/").status_code == 200
        assert Client().get("/api/v1/admin/auth/me/").status_code == 401
