import logging
import secrets
from typing import Any

import redis
from django.conf import settings
from django.core.cache import cache
from django.db import connection
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from glowderma.core import integration_modes, readiness, timeutils
from glowderma.core.api.health_schema import HEALTH_GET
from glowderma.core.api.health_serializers import AWARE_DATETIME
from glowderma.core.network import passes_admin_gate

logger = logging.getLogger(__name__)


def _provider_configuration() -> dict[str, bool]:
    """The live providers' configured flags (``core.readiness``); a probe failure is logged, never raised."""
    try:
        return readiness.live_configured()
    except Exception:
        logger.warning("health check %s failed", "providers", exc_info=True)
        return {}


def _cashback_policy_missing(database_ok: bool) -> bool | None:
    """``core.readiness.cashback_policy_missing`` (registered by loyalty); ``None`` while the database is
    down or when the check fails (logged, never raised)."""
    if not database_ok:
        return None
    try:
        return readiness.cashback_policy_missing()
    except Exception:
        logger.warning("health check %s failed", "cashback policy", exc_info=True)
        return None


def _provider_details(configured: dict[str, bool], database_ok: bool) -> dict[str, dict[str, Any]]:
    syncs: dict[str, Any] = {}
    if database_ok and configured:
        try:
            syncs = readiness.last_syncs()
        except Exception:
            logger.warning("health check %s failed", "provider syncs", exc_info=True)
    now = timeutils.now()
    details: dict[str, dict[str, Any]] = {}
    for provider, ok in configured.items():
        synced = syncs.get(provider)
        age = max(int((now - synced).total_seconds()), 0) if synced is not None else None
        details[provider] = {
            "configured": ok,
            "last_sync_at": AWARE_DATETIME.to_representation(synced) if synced is not None else None,
            "last_sync_age_seconds": age,
        }
    return details


class HealthView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = []

    @HEALTH_GET
    def get(self, request: Request) -> Response:
        if request.query_params.get("ready") != "1":
            return Response({"status": "ok"})
        checks = dict.fromkeys(("database", "cache", "broker"), "ok")
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
        except Exception:
            checks["database"] = "error"
            logger.warning("health check %s failed", "database", exc_info=True)
        try:
            key = f"health:{secrets.token_hex(16)}"
            cache.set(key, "ok", timeout=10)
            if cache.get(key) != "ok":
                raise RuntimeError("Cache round trip failed")
            cache.delete(key)
        except Exception:
            checks["cache"] = "error"
            logger.warning("health check %s failed", "cache", exc_info=True)
        try:
            with redis.Redis.from_url(
                settings.CELERY_BROKER_URL, socket_connect_timeout=1, socket_timeout=1
            ) as broker:
                if not broker.ping():
                    raise RuntimeError("Broker ping failed")
        except Exception:
            checks["broker"] = "error"
            logger.warning("health check %s failed", "broker", exc_info=True)
        configured = _provider_configuration()
        ok = all(value == "ok" for value in checks.values()) and all(configured.values())
        body: dict[str, Any] = {
            "status": "ok" if ok else "error",
            "checks": checks,
            "version": settings.GIT_SHA,
        }
        # Which providers are fake, or live but not configured, is reconnaissance for an attacker (fake
        # payments are allowed in production): only a caller that would pass the admin network gate sees it
        # (contract §2.15); everyone else gets the same status code without the reason.
        if passes_admin_gate(request):
            body["integrations"] = integration_modes.provider_modes()
            body["sandbox"] = integration_modes.sandbox_providers()
            body["providers"] = _provider_details(configured, checks["database"] == "ok")
            body["cashback_policy_missing"] = _cashback_policy_missing(checks["database"] == "ok")
        return Response(body, status=200 if ok else 503)
