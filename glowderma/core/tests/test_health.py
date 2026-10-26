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

    modes = {f"{name}_MODE": "fake" for name in ("OAUTH", "BILLZ", "FCM", "PAYME", "BTS")}
    with override_settings(**modes, CLICK_MODE="live"):
        body = Client().get("/api/v1/health/?ready=1").json()
    assert body["integrations"] == {
        "oauth": "fake",
        "billz": "fake",
        "fcm": "fake",
        "click": "live",
        "payme": "fake",
        "bts": "fake",
    }
    assert set(body) == {"status", "checks", "version", "integrations"}
    assert "integrations" not in Client().get("/api/v1/health/").json()
