"""Admin serializers of `reporting` for the shell (contract §2.10 nav-counts, §2.13 audit log)."""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from glowderma.core import enums, timeutils
from glowderma.core.api.exceptions import field_error
from glowderma.core.api.serializers import ActorSerializer
from glowderma.core.models import AuditEntry
from glowderma.reporting import selectors


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

    @extend_schema_field(ActorSerializer(allow_null=True))
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


# --- dashboard (contract §2.10) -----------------------------------------------------------------------------


class DashboardQuerySerializer(serializers.Serializer[dict[str, Any]]):
    """``range`` (default ``today``); ``from``/``to`` required iff ``custom``: ``from ≤ to ≤ today``
    (Tashkent), span ≤ 366 days."""

    range = serializers.ChoiceField(
        choices=enums.DashboardRange.values, required=False, default=enums.DashboardRange.TODAY
    )
    from_ = serializers.DateField(required=False)
    to = serializers.DateField(required=False)

    def get_fields(self) -> dict[str, serializers.Field[Any, Any, Any, Any]]:
        fields = super().get_fields()
        fields["from"] = fields.pop("from_")  # ``from`` is a Python keyword
        return fields

    def to_internal_value(self, data: Any) -> dict[str, Any]:
        # ``from``/``to`` are ignored (not even parsed) unless ``range=custom`` (contract §2.10).
        if isinstance(data, dict) and data.get("range") != enums.DashboardRange.CUSTOM:
            data = {key: value for key, value in data.items() if key not in ("from", "to")}
        validated: dict[str, Any] = super().to_internal_value(data)
        return validated

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if attrs["range"] != enums.DashboardRange.CUSTOM:
            attrs.pop("from", None)
            attrs.pop("to", None)
            return attrs
        errors: dict[str, list[Any]] = {}
        for name in ("from", "to"):
            if attrs.get(name) is None:
                errors[name] = [field_error("required")]
        if errors:
            raise serializers.ValidationError(errors)
        first, last = attrs["from"], attrs["to"]
        today = timeutils.local_date(timeutils.now())
        if first > last:
            raise serializers.ValidationError({"from": [field_error("invalid")]})
        if last > today:
            raise serializers.ValidationError({"to": [field_error("invalid")]})
        if (last - first).days + 1 > selectors.CUSTOM_MAX_DAYS:
            raise serializers.ValidationError({"to": [field_error("invalid")]})
        return attrs


class DashboardBucketSerializer(serializers.Serializer[dict[str, Any]]):
    start = serializers.DateTimeField()
    revenue = serializers.IntegerField()
    orders = serializers.IntegerField()


class DashboardStatsSerializer(serializers.Serializer[dict[str, Any]]):
    orders = serializers.IntegerField()
    revenue = serializers.IntegerField()
    new_customers = serializers.IntegerField()
    total_customers = serializers.IntegerField()
    deleted_customers = serializers.IntegerField()
    out_of_stock = serializers.IntegerField()


class DashboardAttentionSerializer(serializers.Serializer[dict[str, Any]]):
    bts_failed = serializers.IntegerField()
    pending_appointments = serializers.IntegerField()
    next_appointment_at = serializers.DateTimeField(allow_null=True)
    new_from_billz = serializers.IntegerField()
    refunds_needing_action = serializers.IntegerField()
    bts_polling_failing = serializers.BooleanField(
        help_text="The last BTS polling run tracked no parcel or stopped on transport failures (M2)."
    )


class DashboardSerializer(serializers.Serializer[dict[str, Any]]):
    range = serializers.ChoiceField(choices=enums.DashboardRange.values)
    from_ = serializers.DateField()
    to = serializers.DateField()
    bucket = serializers.ChoiceField(choices=enums.DashboardBucket.values)
    buckets = DashboardBucketSerializer(many=True)
    previous_period_orders = serializers.IntegerField()
    stats = DashboardStatsSerializer()
    attention = DashboardAttentionSerializer()
    generated_at = serializers.DateTimeField()

    def get_fields(self) -> dict[str, serializers.Field[Any, Any, Any, Any]]:
        fields = super().get_fields()
        fields["from"] = fields.pop("from_")
        return fields
