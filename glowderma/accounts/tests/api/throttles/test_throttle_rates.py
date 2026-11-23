import pytest
from django.test import override_settings
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory
from rest_framework.views import APIView

from glowderma.accounts.api.throttles import (
    AuthClientIPThrottle,
    ClientIPThrottle,
    LoginRateThrottle,
    RefreshRateThrottle,
    StaffRateThrottle,
)
from glowderma.core.throttling import rate_for


@pytest.mark.parametrize(
    "throttle_class,expected",
    [
        (LoginRateThrottle, "30/min"),
        (AuthClientIPThrottle, "600/min"),
        (RefreshRateThrottle, "120/min"),
        (StaffRateThrottle, "1200/min"),
    ],
)
def test_throttle_classes_resolve_default_rates(
    throttle_class: type[ClientIPThrottle], expected: str
) -> None:
    """§1.9 "(Redis cache; defaults, configurable)": each class keeps its contract default via rate_for()."""
    throttle = throttle_class()
    assert throttle.rate == expected
    assert throttle.get_rate() == rate_for(throttle.scope)
    assert "rate" not in vars(throttle_class)


@override_settings(THROTTLE_RATES={"login": "2/min"})
def test_login_override_applies_new_limit() -> None:
    """§1.9 login per client IP is configurable: THROTTLE_RATES={"login": "2/min"} limits to two attempts."""
    throttle = LoginRateThrottle()
    view = APIView()
    request = Request(APIRequestFactory().post("/", {"email": "a@b.uz"}, format="json"))
    request.client_ip = "192.0.2.9"  # type: ignore[attr-defined]
    assert throttle.allow_request(request, view)
    assert throttle.allow_request(request, view)
    assert not throttle.allow_request(request, view)
    assert throttle.wait() is not None
    assert AuthClientIPThrottle().rate == "600/min"
