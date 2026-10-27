import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory
from rest_framework.views import APIView

from glowderma.core.exports import ExportRateThrottle
from glowderma.core.throttling import DEFAULT_THROTTLE_RATES, rate_for, validate_throttle_rates


def test_defaults_match_contract_1_9() -> None:
    """§1.9: login per IP 30/min, refresh per session 120/min, refresh/logout per IP 600/min,
    export per staff 10/min, everything else per staff 1200/min, oauth per IP 30/min."""
    assert DEFAULT_THROTTLE_RATES["login"] == "30/min"
    assert DEFAULT_THROTTLE_RATES["refresh"] == "120/min"
    assert DEFAULT_THROTTLE_RATES["auth_client_ip"] == "600/min"
    assert DEFAULT_THROTTLE_RATES["export"] == "10/min"
    assert DEFAULT_THROTTLE_RATES["staff"] == "1200/min"
    assert DEFAULT_THROTTLE_RATES["oauth"] == "30/min"
    assert set(DEFAULT_THROTTLE_RATES) == {
        "login",
        "auth_client_ip",
        "refresh",
        "staff",
        "export",
        "oauth",
        "customer",
        "sms_phone",
        "sms_ip",
        "checkout",
        "pay",
        "pos",
        # M2 (customer contract §1.8)
        "sms_device",
        "customer_auth_device",
        "customer_auth_ip",
        "refresh_unknown",
        "qr",
        "public",
        "public_ip",
    }
    for rate in DEFAULT_THROTTLE_RATES.values():
        validate_throttle_rates({"login": rate})


def test_export_throttle_resolves_rate_through_rate_for() -> None:
    """§1.9 "?export= per staff 10/min" stays the default; the class carries no hard-coded rate."""
    throttle = ExportRateThrottle()
    assert throttle.rate == "10/min" == rate_for("export")
    assert throttle.get_rate() == rate_for(throttle.scope)
    assert "rate" not in vars(ExportRateThrottle)


@override_settings(THROTTLE_RATES={"export": "2/min"})
def test_override_applies_new_limit() -> None:
    """§1.9 "defaults, configurable": settings.THROTTLE_RATES wins over the default for that scope only."""
    assert rate_for("export") == "2/min"
    assert rate_for("staff") == "1200/min"
    throttle = ExportRateThrottle()
    view = APIView()
    request = Request(APIRequestFactory().get("/?export=csv"))
    request.user = type("User", (), {"pk": 7, "is_authenticated": True})()
    assert throttle.allow_request(request, view)
    assert throttle.allow_request(request, view)
    assert not throttle.allow_request(request, view)
    assert throttle.wait() is not None


def test_validate_accepts_drf_rate_formats() -> None:
    rates = {"login": "30/min", "staff": "5/hour", "export": "1/d", "pos": "7/sec"}
    assert validate_throttle_rates(rates) == rates
    assert validate_throttle_rates({}) == {}


@pytest.mark.parametrize(
    "value",
    [
        {"login": "abc"},
        {"login": "0/min"},
        {"login": "30/weeks"},
        {"login": "30min"},
        {"login": 30},
        {"login": "30/min/5"},
        {"unknown_scope": "30/min"},
        ["login", "30/min"],
        "30/min",
    ],
)
def test_validate_rejects_invalid_rates(value: object) -> None:
    """Invalid rate string → ImproperlyConfigured (checked at settings import)."""
    with pytest.raises(ImproperlyConfigured, match="THROTTLE_RATES"):
        validate_throttle_rates(value)


def test_rate_for_unknown_scope_is_improperly_configured() -> None:
    with pytest.raises(ImproperlyConfigured, match="nope"):
        rate_for("nope")
    with pytest.raises(ImproperlyConfigured):
        rate_for(None)


@override_settings(THROTTLE_RATES="not-a-dict")
def test_rate_for_ignores_non_dict_override() -> None:
    assert rate_for("login") == "30/min"
