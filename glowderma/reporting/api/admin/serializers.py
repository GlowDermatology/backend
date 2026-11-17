"""Admin serializers of `reporting` for the shell (contract §2.10 nav-counts, §2.13 audit log)."""

from __future__ import annotations

import re
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
    ip = serializers.SerializerMethodField(
        help_text="Client IP; null for system rows and always null for ordinary staff (managers and "
        "superusers see it)."
    )
    request_id = serializers.CharField()

    def _viewer(self) -> Any:
        request = self.context.get("request")
        return getattr(request, "user", None)

    @extend_schema_field(ActorSerializer(allow_null=True))
    def get_actor(self, obj: AuditEntry) -> dict[str, Any] | None:
        return selectors.audit_actor(obj)

    @extend_schema_field(serializers.JSONField(allow_null=True))
    def get_data(self, obj: AuditEntry) -> Any:
        # without a request (no viewer) the data is masked like for ordinary staff: fail closed
        return selectors.audit_data(obj, self._viewer() or object())

    @extend_schema_field(serializers.IPAddressField(allow_null=True))
    def get_ip(self, obj: AuditEntry) -> str | None:
        return selectors.audit_ip(obj, self._viewer() or object())


REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9-]{8,64}")


class AuditLogQuerySerializer(serializers.Serializer[dict[str, Any]]):
    actor = serializers.IntegerField(required=False, min_value=1)
    action = serializers.CharField(required=False, max_length=100)
    object_type = serializers.CharField(required=False, max_length=100)
    object_id = serializers.CharField(required=False, max_length=254)
    request_id = serializers.CharField(
        required=False,
        help_text="The start of a request id (8–64 characters `A-Z a-z 0-9 -`): entries whose stored "
        "request id begins with it.",
    )
    date_from = serializers.DateField(required=False)
    date_to = serializers.DateField(required=False)
    ordering = serializers.ChoiceField(choices=["-at", "at"], required=False, default="-at")

    def validate_request_id(self, value: str) -> str:
        if REQUEST_ID_PATTERN.fullmatch(value) is None:
            raise serializers.ValidationError([field_error("invalid")])
        return value


class AuditFacetsSerializer(serializers.Serializer[dict[str, Any]]):
    """``GET audit-log/facets/``: what the audit log shows the signed-in staff member."""

    tier = serializers.ChoiceField(choices=enums.AuditViewerTier.values)
    ip_visible = serializers.BooleanField(help_text="Whether `ip` (and network keys in `data`) are shown.")
    visible_prefixes = serializers.ListField(
        child=serializers.CharField(),
        help_text="Action prefixes (the part before the first dot) this viewer sees entries of.",
    )
    own_only_prefixes = serializers.ListField(
        child=serializers.CharField(),
        help_text="The visible prefixes limited to the viewer's own entries (staff sign-ins, exports); "
        "`auth` entries written by customers stay visible to everyone.",
    )
    hidden_actions = serializers.ListField(
        child=serializers.CharField(),
        help_text="Actions hidden although their prefix is visible (operator-only fixes).",
    )


class NavCountsSerializer(serializers.Serializer[dict[str, int]]):
    orders_processing = serializers.IntegerField()
    appointments_pending = serializers.IntegerField()
    delivery_issues_open = serializers.IntegerField(help_text="Open delivery issues (the exception queue).")


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
    cashback_policy_missing = serializers.BooleanField(
        help_text="No cashback policy version is in force now (none exists, or only a future one): purchases "
        "earn no cashback until one starts."
    )
    yandex_failing = serializers.BooleanField(
        help_text="Yandex Delivery is live and its last status poll or price check failed (always false in "
        "fake mode)."
    )


class DashboardFulfilmentSerializer(serializers.Serializer[dict[str, int]]):
    """Order-state counters as the orders stand now (not windowed by ``range``)."""

    paid_to_prepare = serializers.IntegerField(
        help_text="Processing orders still to be prepared: Yandex `awaiting_dispatch` (or `failed` / "
        "`returned` waiting for a new dispatch), BTS not handed over, pickup."
    )
    to_hand_to_courier = serializers.IntegerField(
        help_text="Processing Yandex orders whose courier is requested, assigned or arrived."
    )
    in_delivery = serializers.IntegerField(help_text="Handed to the courier and not coming back.")
    returning = serializers.IntegerField(help_text="Yandex `returning` / `returned` while the order is open.")
    completed_today = serializers.IntegerField(help_text="Delivered today (Asia/Tashkent), pickup included.")
    needs_action = serializers.IntegerField(help_text="Open delivery issues.")
    urgent = serializers.IntegerField(help_text="Open urgent delivery issues.")


class DashboardSerializer(serializers.Serializer[dict[str, Any]]):
    range = serializers.ChoiceField(choices=enums.DashboardRange.values)
    from_ = serializers.DateField()
    to = serializers.DateField()
    bucket = serializers.ChoiceField(choices=enums.DashboardBucket.values)
    buckets = DashboardBucketSerializer(many=True)
    previous_period_orders = serializers.IntegerField()
    stats = DashboardStatsSerializer()
    attention = DashboardAttentionSerializer()
    fulfilment = DashboardFulfilmentSerializer()
    generated_at = serializers.DateTimeField()

    def get_fields(self) -> dict[str, serializers.Field[Any, Any, Any, Any]]:
        fields = super().get_fields()
        fields["from"] = fields.pop("from_")
        return fields
