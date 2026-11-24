"""Customer segments: the row, the write and the manual members body."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers
from rest_framework.utils import html

from glowderma.accounts.models import Customer
from glowderma.core import enums
from glowderma.core.api.exceptions import field_error
from glowderma.core.api.fields import (
    BoundedListField,
    LenientPhoneField,
)
from glowderma.notifications import selectors
from glowderma.notifications.api.admin.serializers.messages import MESSAGES
from glowderma.notifications.models import CustomerSegment


class SegmentRulesSerializer(serializers.Serializer[dict[str, Any]]):
    min_online_orders = serializers.IntegerField(min_value=1, allow_null=True, required=False)
    min_lifetime_amount = serializers.IntegerField(min_value=1, allow_null=True, required=False)


class SegmentSerializer(serializers.Serializer[CustomerSegment]):
    """Contract §2.8 ``Segment``; the counts come from ``selectors.segment_stats``."""

    id = serializers.IntegerField()
    name = serializers.CharField()
    rules = serializers.SerializerMethodField()
    manual_member_count = serializers.SerializerMethodField()
    member_count = serializers.SerializerMethodField()
    size = serializers.SerializerMethodField()
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()

    def _stats(self, obj: CustomerSegment) -> selectors.SegmentStats:
        context = cast(dict[str, Any], self.context)
        cache: dict[int, selectors.SegmentStats] = context.setdefault("segment_stats", {})
        if obj.pk not in cache:
            cache[obj.pk] = selectors.segment_stats(obj)
        return cache[obj.pk]

    @extend_schema_field(SegmentRulesSerializer())
    def get_rules(self, obj: CustomerSegment) -> dict[str, Any]:
        return {"min_online_orders": obj.min_online_orders, "min_lifetime_amount": obj.min_lifetime_amount}

    @extend_schema_field(serializers.IntegerField())
    def get_manual_member_count(self, obj: CustomerSegment) -> int:
        return self._stats(obj)["manual_member_count"]

    @extend_schema_field(serializers.IntegerField())
    def get_member_count(self, obj: CustomerSegment) -> int:
        return self._stats(obj)["member_count"]

    @extend_schema_field(serializers.IntegerField())
    def get_size(self, obj: CustomerSegment) -> int:
        return self._stats(obj)["size"]


class SegmentWriteSerializer(serializers.Serializer[dict[str, Any]]):
    """``POST segments/`` (``name`` 2..60 unique, ``rules``) and ``PATCH segments/{id}/`` (partial)."""

    name = serializers.CharField(min_length=2, max_length=60)
    rules = SegmentRulesSerializer(required=False)

    def __init__(self, *args: Any, instance: CustomerSegment | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.segment = instance

    def validate_name(self, value: str) -> str:
        value = value.strip()
        if len(value) < 2:
            raise serializers.ValidationError([field_error("min_length", limit=2)])
        taken = CustomerSegment.objects.filter(name__iexact=value)
        if self.segment is not None:
            taken = taken.exclude(pk=self.segment.pk)
        if taken.exists():
            raise serializers.ValidationError([field_error("unique")])
        return value

    def to_changes(self) -> dict[str, Any]:
        data = self.validated_data
        changes: dict[str, Any] = {}
        if "name" in data:
            changes["name"] = data["name"]
        for key in ("min_online_orders", "min_lifetime_amount"):
            if key in data.get("rules", {}):
                changes[key] = data["rules"][key]
        return changes


MEMBERS_MAX_ITEMS = 500


class CustomerIdsField(BoundedListField):
    """A members list (``customer_ids`` / ``phones``): DRF's ``not_a_list`` is not a contract code (and
    carries English stock text), so it answers the generic ``invalid`` (contract §1.6). An empty list is
    returned as ``[]``: the serializer answers ``required`` when neither list names anybody."""

    def to_internal_value(self, data: Any) -> list[Any]:
        if html.is_html_input(data):
            data = html.parse_html_list(data, default=[])
        if isinstance(data, (str, bytes, Mapping)) or not hasattr(data, "__iter__"):
            raise serializers.ValidationError([field_error("invalid")])
        if len(data) == 0:
            return []
        return super().to_internal_value(data)


# ``validated_data`` carries the resolved, sorted ``customer_ids`` only (the union of both lists).
class SegmentMembersInputSerializer(serializers.Serializer[dict[str, Any]]):
    """``POST segments/{id}/members/`` (contract §2.8): ``customer_ids`` (any status, must exist) and/or
    ``phones`` (E.164, resolved to **active** customers), each ≤ 500 and deduplicated."""

    customer_ids = CustomerIdsField(
        child=serializers.IntegerField(), max_items=MEMBERS_MAX_ITEMS, required=False
    )
    phones = CustomerIdsField(child=LenientPhoneField(), max_items=MEMBERS_MAX_ITEMS, required=False)

    def validate_customer_ids(self, value: list[int]) -> list[int]:
        ids = sorted(set(value))
        known = set(Customer.objects.filter(pk__in=ids).values_list("pk", flat=True))
        if len(known) != len(ids):
            raise serializers.ValidationError([field_error("does_not_exist")])
        return ids

    def validate_phones(self, value: list[str]) -> list[int]:
        first_index: dict[str, int] = {}
        for index, phone in enumerate(value):
            first_index.setdefault(phone, index)
        found = dict(
            Customer.objects.filter(
                phone__in=list(first_index), status=enums.CustomerStatus.ACTIVE
            ).values_list("phone", "pk")
        )
        missing: dict[str, Any] = {
            str(index): [field_error("customer_not_found", MESSAGES["customer_not_found"])]
            for phone, index in first_index.items()
            if phone not in found
        }
        if missing:
            raise serializers.ValidationError(missing)
        return sorted(set(found.values()))

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        ids = set(attrs.pop("customer_ids", [])) | set(attrs.pop("phones", []))
        if not ids:
            raise serializers.ValidationError({"customer_ids": [field_error("required")]})
        attrs["customer_ids"] = sorted(ids)
        return attrs


__all__ = [
    "MEMBERS_MAX_ITEMS",
    "CustomerIdsField",
    "SegmentMembersInputSerializer",
    "SegmentRulesSerializer",
    "SegmentSerializer",
    "SegmentWriteSerializer",
]
