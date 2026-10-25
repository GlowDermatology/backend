"""Admin serializers of `reporting` for the shell (contract §2.10 nav-counts, §2.13 audit log)."""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from glowderma.core.models import AuditEntry
from glowderma.reporting import selectors
from glowderma.site_config.api.admin.serializers import ActorRefSerializer


class AuditEntrySerializer(serializers.Serializer[AuditEntry]):
    id = serializers.IntegerField()
    at = serializers.DateTimeField(source="created_at")
    actor = serializers.SerializerMethodField()
    action = serializers.CharField()
    object_type = serializers.CharField(source="entity_type")
    object_id = serializers.CharField(source="entity_id")
    data = serializers.SerializerMethodField()  # type: ignore[assignment]  # a field, not Serializer.data
    ip = serializers.IPAddressField(allow_null=True)
    request_id = serializers.CharField()

    @extend_schema_field(ActorRefSerializer(allow_null=True))
    def get_actor(self, obj: AuditEntry) -> dict[str, Any] | None:
        return selectors.audit_actor(obj)

    @extend_schema_field(serializers.JSONField(allow_null=True))
    def get_data(self, obj: AuditEntry) -> Any:
        return selectors.audit_data(obj)


class AuditLogQuerySerializer(serializers.Serializer[dict[str, Any]]):
    actor = serializers.IntegerField(required=False, min_value=1)
    action = serializers.CharField(required=False, max_length=100)
    object_type = serializers.CharField(required=False, max_length=100)
    object_id = serializers.CharField(required=False, max_length=254)
    date_from = serializers.DateField(required=False)
    date_to = serializers.DateField(required=False)
    ordering = serializers.ChoiceField(choices=["-at", "at"], required=False, default="-at")


class NavCountsSerializer(serializers.Serializer[dict[str, int]]):
    orders_processing = serializers.IntegerField()
    appointments_pending = serializers.IntegerField()
