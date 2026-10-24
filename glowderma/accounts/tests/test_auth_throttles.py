import pytest
from rest_framework.parsers import JSONParser
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from glowderma.accounts.api.admin.views import LogoutView, RefreshView
from glowderma.accounts.api.base import AdminAPIView, AdminGenericViewSet


@pytest.mark.parametrize("view_class", [RefreshView, LogoutView])
def test_refresh_and_logout_have_client_ip_ceiling(view_class: type[RefreshView]) -> None:
    view = view_class()
    throttles = view.get_throttles()
    ip_limits = [throttle for throttle in throttles if getattr(throttle, "rate", None) == "600/min"]
    assert len(ip_limits) == 1
    throttle = ip_limits[0]
    request = Request(APIRequestFactory().post("/", {"refresh": "test"}, format="json"))
    request.client_ip = "192.0.2.1"  # type: ignore[attr-defined]
    for _ in range(600):
        assert throttle.allow_request(request, view)
    assert not throttle.allow_request(request, view)
    request.client_ip = "192.0.2.2"  # type: ignore[attr-defined]
    assert throttle.allow_request(request, view)


def test_admin_bases_default_to_json() -> None:
    assert AdminAPIView.parser_classes == [JSONParser]
    assert AdminGenericViewSet.parser_classes == [JSONParser]
