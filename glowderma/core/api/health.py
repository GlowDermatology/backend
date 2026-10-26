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

from glowderma.core import integration_modes
from glowderma.core.api.serializers import ProviderModesSerializer
from glowderma.core.network import passes_admin_gate

logger = logging.getLogger(__name__)


class HealthSerializer(serializers.Serializer[dict[str, Any]]):
    status = serializers.ChoiceField(choices=["ok", "error"])
    checks = serializers.DictField(child=serializers.ChoiceField(choices=["ok", "error"]), required=False)
    version = serializers.CharField(required=False)
    integrations = ProviderModesSerializer(
        required=False,
        help_text="`?ready=1` only, and only for a caller that passes the admin network gate (§1): the mode "
        "of every provider with a `*_MODE` setting.",
    )


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
        ok = all(value == "ok" for value in checks.values())
        body: dict[str, Any] = {
            "status": "ok" if ok else "error",
            "checks": checks,
            "version": settings.GIT_SHA,
        }
        # Which providers are fake is reconnaissance for an attacker (fake payments are allowed in
        # production): only a caller that would pass the admin network gate sees it (contract §2.15).
        if passes_admin_gate(request):
            body["integrations"] = integration_modes.provider_modes()
        return Response(body, status=200 if ok else 503)
