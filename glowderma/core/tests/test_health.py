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
        response = Client().get("/api/v1/health/?ready=1")
    body = response.json()
    assert body["integrations"] == {
        "oauth": "fake",
        "billz": "fake",
        "fcm": "fake",
        "click": "live",
        "payme": "fake",
        "bts": "fake",
        "eskiz": "fake",
    }
    assert set(body) == {
        "status",
        "checks",
        "version",
        "integrations",
        "sandbox",
        "providers",
        "cashback_policy_missing",
    }
    assert body["sandbox"] == []
    # live Click without its merchant settings (the test settings leave them empty) is not configured, so
    # readiness fails for every caller
    assert response.status_code == 503 and body["status"] == "error"
    assert body["providers"] == {
        "click": {"configured": False, "last_sync_at": None, "last_sync_age_seconds": None}
    }
    assert body["checks"] == dict.fromkeys(("database", "cache", "broker"), "ok")
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


# --- M3 readiness: live providers' configured flags and sync ages (docs/ops/monitoring.md) -----------------

ALL_FAKE = {f"{name}_MODE": "fake" for name in ("OAUTH", "BILLZ", "FCM", "CLICK", "PAYME", "BTS", "ESKIZ")}
LIVE_BILLZ = {
    "BILLZ_MODE": "live",
    "BILLZ_SECRET_KEY": "billz-integration-key",
    "BILLZ_SHOP_ID": "6b3c0e9a-1d2f-4a5b-8c7d-9e0f1a2b3c4d",
    "BILLZ_CASHBOX_ID": "7c4d1f0b-2e3a-4b6c-9d8e-0f1a2b3c4d5e",
    "BILLZ_PAYMENT_TYPE_ID": "8d5e2a1c-3f4b-4c7d-8e9f-1a2b3c4d5e6f",
}


@pytest.mark.django_db
def test_ready_with_every_provider_fake_lists_no_provider() -> None:
    from django.test import override_settings

    with override_settings(**ALL_FAKE):
        response = Client().get("/api/v1/health/?ready=1")
    assert response.status_code == 200 and response.json()["providers"] == {}


@pytest.mark.django_db
def test_ready_reports_a_configured_live_billz_with_its_sync_age(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import datetime, timedelta

    from django.test import override_settings

    from glowderma.core import timeutils
    from glowderma.integrations.models import IntegrationState

    now = datetime.fromisoformat("2026-10-27T10:00:00+05:00")
    monkeypatch.setattr(timeutils, "now", lambda: now)
    IntegrationState.objects.filter(provider="billz").update(
        last_sync_at=now - timedelta(minutes=53, seconds=7)
    )
    with override_settings(**{**ALL_FAKE, **LIVE_BILLZ}):
        response = Client().get("/api/v1/health/?ready=1")
    assert response.status_code == 200
    assert response.json()["providers"] == {
        "billz": {
            "configured": True,
            "last_sync_at": "2026-10-27T09:06:53+05:00",
            "last_sync_age_seconds": 3187,
        }
    }


@pytest.mark.django_db
def test_a_live_provider_that_is_not_configured_fails_readiness_for_everyone() -> None:
    """Outside the admin gate the 503 carries no reason (which provider is unconfigured is reconnaissance)."""
    from django.test import override_settings

    unconfigured = {**ALL_FAKE, **LIVE_BILLZ, "BILLZ_SHOP_ID": ""}
    with override_settings(**unconfigured, ADMIN_API_ALLOWED_NETS=[ip_network("10.0.0.0/8")]):
        outside = Client(REMOTE_ADDR="203.0.113.7").get("/api/v1/health/?ready=1")
        inside = Client(REMOTE_ADDR="10.1.2.3").get("/api/v1/health/?ready=1")
        shallow = Client(REMOTE_ADDR="203.0.113.7").get("/api/v1/health/")
    assert outside.status_code == inside.status_code == 503
    assert outside.json() == {
        "status": "error",
        "checks": dict.fromkeys(("database", "cache", "broker"), "ok"),
        "version": settings.GIT_SHA,
    }
    assert inside.json()["providers"]["billz"] == {
        "configured": False,
        "last_sync_at": None,
        "last_sync_age_seconds": None,
    }
    assert shallow.status_code == 200 and shallow.json() == {"status": "ok"}


@pytest.mark.django_db
def test_a_failing_probe_or_sync_read_is_logged_never_raised(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from datetime import datetime

    from django.test import override_settings

    from glowderma.core import readiness

    class Broken:
        def configured(self) -> dict[str, bool]:
            raise RuntimeError("probe broke")

        def last_syncs(self) -> dict[str, datetime | None]:
            raise RuntimeError("sync read broke")

    class Half(Broken):
        def configured(self) -> dict[str, bool]:
            return {"billz": True}

    original = readiness.probe()
    try:
        readiness.register(Broken())
        with override_settings(**ALL_FAKE):
            broken = Client().get("/api/v1/health/?ready=1")
        readiness.register(Half())
        half = Client().get("/api/v1/health/?ready=1")
    finally:
        assert original is not None
        readiness.register(original)
    assert broken.status_code == 200 and broken.json()["providers"] == {}
    assert half.status_code == 200
    assert half.json()["providers"] == {
        "billz": {"configured": True, "last_sync_at": None, "last_sync_age_seconds": None}
    }
    assert "health check providers failed" in caplog.text
    assert "health check provider syncs failed" in caplog.text


def test_readiness_without_a_probe_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    from glowderma.core import readiness

    monkeypatch.setattr(readiness, "_probe", None)
    assert readiness.probe() is None
    assert readiness.live_configured() == {} and readiness.last_syncs() == {}


@pytest.mark.django_db
def test_ready_reports_a_missing_cashback_policy_inside_the_gate_only() -> None:
    """Gap CB-9.2.4: no policy in force → ``cashback_policy_missing`` for operators; never a 503."""
    from datetime import timedelta

    from django.test import override_settings
    from django.utils import timezone

    from glowderma.loyalty.tests.factories import CashbackPolicyFactory

    with override_settings(**ALL_FAKE, ADMIN_API_ALLOWED_NETS=[ip_network("10.0.0.0/8")]):
        missing = Client(REMOTE_ADDR="10.1.2.3").get("/api/v1/health/?ready=1")
        outside = Client(REMOTE_ADDR="203.0.113.7").get("/api/v1/health/?ready=1")
        CashbackPolicyFactory.create(effective_from=timezone.now() + timedelta(days=3))  # scheduled only
        scheduled = Client(REMOTE_ADDR="10.1.2.3").get("/api/v1/health/?ready=1")
        CashbackPolicyFactory.create(
            version=2,
            effective_from=timezone.now() - timedelta(days=1),
            created_at=timezone.now() - timedelta(days=2),
        )
        active = Client(REMOTE_ADDR="10.1.2.3").get("/api/v1/health/?ready=1")
    assert missing.status_code == 200 and missing.json()["status"] == "ok"
    assert missing.json()["cashback_policy_missing"] is True
    assert "cashback_policy_missing" not in outside.json()
    assert scheduled.json()["cashback_policy_missing"] is True
    assert active.status_code == 200 and active.json()["cashback_policy_missing"] is False


@pytest.mark.django_db
def test_the_cashback_policy_check_is_null_while_unknown(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from django.test import override_settings

    from glowderma.core import readiness

    with override_settings(**ALL_FAKE):
        with patch("glowderma.core.api.health.connection.cursor", side_effect=RuntimeError("db down")):
            down = Client().get("/api/v1/health/?ready=1")

        def broken() -> bool:
            raise RuntimeError("check broke")

        monkeypatch.setattr(readiness, "_cashback_policy_check", broken)
        failing = Client().get("/api/v1/health/?ready=1")
        monkeypatch.setattr(readiness, "_cashback_policy_check", None)
        unregistered = Client().get("/api/v1/health/?ready=1")
    assert down.status_code == 503 and down.json()["cashback_policy_missing"] is None
    assert failing.status_code == 200 and failing.json()["cashback_policy_missing"] is None
    assert "health check cashback policy failed" in caplog.text
    assert unregistered.status_code == 200 and unregistered.json()["cashback_policy_missing"] is None
