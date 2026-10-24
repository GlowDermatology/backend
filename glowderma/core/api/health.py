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


class HealthSerializer(serializers.Serializer[dict[str, Any]]):
    status = serializers.ChoiceField(choices=["ok", "error"])
    checks = serializers.DictField(child=serializers.ChoiceField(choices=["ok", "error"]), required=False)
    version = serializers.CharField(required=False)


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
        try:
            key = f"health:{secrets.token_hex(16)}"
            cache.set(key, "ok", timeout=10)
            if cache.get(key) != "ok":
                checks["cache"] = "error"
            cache.delete(key)
        except Exception:
            checks["cache"] = "error"
        try:
            with redis.Redis.from_url(
                settings.CELERY_BROKER_URL, socket_connect_timeout=1, socket_timeout=1
            ) as broker:
                if not broker.ping():
                    checks["broker"] = "error"
        except Exception:
            checks["broker"] = "error"
        ok = all(value == "ok" for value in checks.values())
        return Response(
            {"status": "ok" if ok else "error", "checks": checks, "version": settings.GIT_SHA},
            status=200 if ok else 503,
        )
