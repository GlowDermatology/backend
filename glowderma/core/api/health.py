import logging
import secrets
from typing import Any

import redis
from django.conf import settings
from django.core.cache import cache
from django.db import connection
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from glowderma.core import integration_modes, readiness, timeutils
from glowderma.core.api.serializers import ProviderModesSerializer
from glowderma.core.network import passes_admin_gate

logger = logging.getLogger(__name__)
# Renders a datetime as the API does (Tashkent, seconds, "+05:00").
_DATETIME = serializers.DateTimeField()


class ProviderReadinessSerializer(serializers.Serializer[dict[str, Any]]):
    configured = serializers.BooleanField(
        help_text="The live adapter can be built and its `is_configured()` (where the port has one) says "
        "yes; `false` makes readiness answer 503."
    )
    last_sync_at = serializers.DateTimeField(
        allow_null=True,
        help_text="Billz and BTS only: the last successful catalog sync / polling run; `null` otherwise.",
    )
    last_sync_age_seconds = serializers.IntegerField(
        allow_null=True, min_value=0, help_text="Seconds since `last_sync_at`; `null` with it."
    )


class HealthSerializer(serializers.Serializer[dict[str, Any]]):
    status = serializers.ChoiceField(choices=["ok", "error"])
    checks = serializers.DictField(child=serializers.ChoiceField(choices=["ok", "error"]), required=False)
    version = serializers.CharField(required=False)
    integrations = ProviderModesSerializer(
        required=False,
        help_text="`?ready=1` only, and only for a caller that passes the admin network gate (§1): the mode "
        "of every provider with a `*_MODE` setting.",
    )
    sandbox = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        help_text="With `integrations`: the live providers configured against a sandbox (BTS test host, "
        "Payme sandbox checkout / `PAYME_TEST_MODE`, Eskiz test sender); empty in a real deployment.",
    )
    providers = serializers.DictField(
        child=ProviderReadinessSerializer(),
        required=False,
        help_text="`?ready=1` only, and only for a caller that passes the admin network gate (§1): one entry "
        "per provider whose mode is `live`. A live provider that is not configured makes `status` `error` "
        "(503) for every caller.",
    )


def _provider_configuration() -> dict[str, bool]:
    """The live providers' configured flags (``core.readiness``); a probe failure is logged, never raised."""
    try:
        return readiness.live_configured()
    except Exception:
        logger.warning("health check %s failed", "providers", exc_info=True)
        return {}


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
            "last_sync_at": _DATETIME.to_representation(synced) if synced is not None else None,
            "last_sync_age_seconds": age,
        }
    return details


class HealthView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = []

    @extend_schema(
        operation_id="health_retrieve",
        auth=[],
        parameters=[OpenApiParameter("ready", OpenApiTypes.BOOL)],
        responses={200: HealthSerializer, 503: HealthSerializer},
    )
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
        return Response(body, status=200 if ok else 503)
