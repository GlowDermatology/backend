from ipaddress import ip_network
from unittest.mock import patch

import pytest
from django.conf import settings
from django.test import Client


def test_health_ok() -> None:
    response = Client().get("/api/v1/health/")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.django_db
def test_health_ready_ok() -> None:
    response = Client().get("/api/v1/health/?ready=1")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["version"] == settings.GIT_SHA
    assert response.json()["checks"] == dict.fromkeys(("database", "cache", "broker"), "ok")


@pytest.mark.django_db
def test_health_ready_db_failure_503(caplog: pytest.LogCaptureFixture) -> None:
    with patch("glowderma.core.api.health.connection.cursor", side_effect=RuntimeError("private-db-error")):
        response = Client().get("/api/v1/health/?ready=1")
    assert response.status_code == 503
    assert response.json()["status"] == "error"
    assert response.json()["checks"] == {"database": "error", "cache": "ok", "broker": "ok"}
    assert "private-db-error" not in str(response.json())
    assert any(r.getMessage() == "health check database failed" and r.exc_info for r in caplog.records)


@pytest.mark.django_db
@pytest.mark.parametrize("check,target", [("cache", "cache.get"), ("broker", "redis.Redis.from_url")])
def test_health_ready_other_failure_503(check: str, target: str, caplog: pytest.LogCaptureFixture) -> None:
    with patch(f"glowderma.core.api.health.{target}", side_effect=RuntimeError("private-service-error")):
        response = Client().get("/api/v1/health/?ready=1")
    expected = dict.fromkeys(("database", "cache", "broker"), "ok")
    expected[check] = "error"
    assert response.status_code == 503
    assert response.json()["status"] == "error"
    assert response.json()["checks"] == expected
    assert "private-service-error" not in str(response.json())
    assert any(r.getMessage() == f"health check {check} failed" and r.exc_info for r in caplog.records)


@pytest.mark.django_db
def test_health_ready_lists_every_provider_mode() -> None:
    from django.test import override_settings

    modes = {f"{name}_MODE": "fake" for name in ("OAUTH", "BILLZ", "FCM", "PAYME", "BTS", "ESKIZ")}
    with override_settings(**modes, CLICK_MODE="live"):
        body = Client().get("/api/v1/health/?ready=1").json()
    assert body["integrations"] == {
        "oauth": "fake",
        "billz": "fake",
        "fcm": "fake",
        "click": "live",
        "payme": "fake",
        "bts": "fake",
        "eskiz": "fake",
    }
    assert set(body) == {"status", "checks", "version", "integrations", "sandbox"} and body["sandbox"] == []
    assert "integrations" not in Client().get("/api/v1/health/").json()


@pytest.mark.django_db
def test_health_ready_flags_live_providers_on_a_sandbox() -> None:
    """R9 / nit: BTS's test host, PAYME_TEST_MODE and Eskiz's test sender are visible to operators."""
    from django.test import override_settings

    with override_settings(
        BTS_MODE="live",
        BTS_BASE_URL="https://apitest.bts.uz:28345",
        PAYME_MODE="live",
        PAYME_TEST_MODE=True,
        ESKIZ_MODE="live",
        ESKIZ_SENDER="4546",
    ):
        assert Client().get("/api/v1/health/?ready=1").json()["sandbox"] == ["bts", "payme", "eskiz"]
    with override_settings(
        BTS_MODE="live",
        BTS_BASE_URL="https://api.bts.uz",
        PAYME_MODE="live",
        PAYME_TEST_MODE=False,
        PAYME_CHECKOUT_URL="https://checkout.paycom.uz",
        ESKIZ_MODE="live",
        ESKIZ_SENDER="GlowDerma",
    ):
        assert Client().get("/api/v1/health/?ready=1").json()["sandbox"] == []


@pytest.mark.django_db
def test_health_ready_hides_the_provider_modes_outside_the_admin_gate() -> None:
    """Contract §2.15: which providers are fake is reconnaissance; only a caller that passes the admin
    network gate (§1) sees ``integrations``. Status, checks and version stay public."""
    from django.test import override_settings

    secret = settings.INTERNAL_API_SECRET
    with override_settings(ADMIN_API_ALLOWED_NETS=[ip_network("10.0.0.0/8")]):
        outside = Client(REMOTE_ADDR="203.0.113.7").get("/api/v1/health/?ready=1")
        inside = Client(REMOTE_ADDR="10.1.2.3").get("/api/v1/health/?ready=1")
    assert outside.status_code == 200 and set(outside.json()) == {"status", "checks", "version"}
    assert "integrations" in inside.json()
    with override_settings(
        ADMIN_API_ALLOWED_NETS=[ip_network("10.0.0.0/8")], ADMIN_API_REQUIRE_INTERNAL_SECRET=True
    ):
        bare = Client(REMOTE_ADDR="10.1.2.3").get("/api/v1/health/?ready=1")
        wrong = Client(REMOTE_ADDR="10.1.2.3").get(
            "/api/v1/health/?ready=1",
            HTTP_X_GD_INTERNAL_SECRET="nope",  # noqa: S106
        )
        signed = Client(REMOTE_ADDR="10.1.2.3").get(
            "/api/v1/health/?ready=1", HTTP_X_GD_INTERNAL_SECRET=secret
        )
        outsider = Client(REMOTE_ADDR="203.0.113.7").get(
            "/api/v1/health/?ready=1", HTTP_X_GD_INTERNAL_SECRET=secret
        )
    assert "integrations" not in bare.json() and "integrations" not in wrong.json()
    assert "integrations" not in outsider.json()
    assert signed.json()["integrations"]["click"] in ("fake", "live")


def test_provider_modes_serializer_follows_the_settings_map(monkeypatch: pytest.MonkeyPatch) -> None:
    """One field per ``PROVIDER_MODE_SETTINGS`` entry: health and ``GET settings/`` cannot drift from it."""
    from glowderma.core import integration_modes
    from glowderma.core.api.serializers import ProviderModesSerializer

    assert set(ProviderModesSerializer().fields) == set(integration_modes.PROVIDER_MODE_SETTINGS)
    monkeypatch.setitem(integration_modes.PROVIDER_MODE_SETTINGS, "sms", "SMS_MODE")
    assert list(ProviderModesSerializer().fields) == [
        "oauth",
        "billz",
        "fcm",
        "click",
        "payme",
        "bts",
        "eskiz",
        "sms",
    ]
