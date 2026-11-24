"""The delivery exception queue (plan §3.9): the issue, its query and bodies, and the dispatch suspension."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from glowderma.commerce.api.admin.serializers.messages import MESSAGE_REASON_MIN
from glowderma.core import enums, timeutils
from glowderma.core.api.exceptions import field_error
from glowderma.core.api.fields import AwareDateTimeField, QueryBooleanField
from glowderma.core.api.serializers import ActorSerializer
from glowderma.core.uzsource import N_
from glowderma.delivery.models import DeliveryIssue

ISSUE_NOTE_MIN_LENGTH = 3


ISSUE_NOTE_MAX_LENGTH = 1000


SUSPENSION_NOTE_MAX_LENGTH = 300


MESSAGE_NOTE = N_("Izohni kiriting.")


MESSAGE_NOTE_MAX = N_("1000 belgidan oshmasin.")


MESSAGE_SUSPENSION_NOTE_MAX = N_("300 belgidan oshmasin.")


MESSAGE_UNTIL_PAST = N_("Kelajakdagi vaqtni tanlang.")


class DeliveryIssueSerializer(serializers.Serializer[DeliveryIssue]):
    """One row of the exception queue: ``details`` holds ids and amounts only; ``resolved_by`` is ``null``
    while open or when the system closed it."""

    id = serializers.IntegerField()
    order_id = serializers.IntegerField()
    kind = serializers.ChoiceField(choices=enums.DeliveryIssueKind.values)
    code = serializers.CharField()
    urgent = serializers.BooleanField()
    details = serializers.JSONField()
    note = serializers.CharField(allow_blank=True)
    state = serializers.ChoiceField(choices=enums.DeliveryIssueState.values)
    opened_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()
    reminded_at = serializers.DateTimeField(allow_null=True)
    reminders = serializers.IntegerField()
    resolved_at = serializers.DateTimeField(allow_null=True)
    resolved_by = ActorSerializer(allow_null=True)
    resolution_note = serializers.CharField(allow_blank=True)


class DeliveryIssueQuerySerializer(serializers.Serializer[dict[str, Any]]):
    state = serializers.ChoiceField(choices=enums.DeliveryIssueState.values, required=False)
    urgent = QueryBooleanField(required=False)
    kind = serializers.ChoiceField(choices=enums.DeliveryIssueKind.values, required=False)
    order = serializers.IntegerField(required=False, min_value=1)


def clean_note(value: str, *, required: bool) -> str:
    text = value.strip()
    if required and len(text) < ISSUE_NOTE_MIN_LENGTH:
        raise serializers.ValidationError(field_error("min_length", MESSAGE_REASON_MIN))
    if len(text) > ISSUE_NOTE_MAX_LENGTH:
        raise serializers.ValidationError(field_error("max_length", MESSAGE_NOTE_MAX))
    return text


class OpenIssueSerializer(serializers.Serializer[dict[str, Any]]):
    """``POST orders/{id}/issues/``: staff open an ``incident`` (a wrong or missing product…); every other
    kind is opened by the system only."""

    kind = serializers.CharField(help_text="`incident` (the only kind staff may open)")
    note = serializers.CharField(allow_blank=True, trim_whitespace=True)
    urgent = serializers.BooleanField(required=False, default=False)

    def validate_kind(self, value: str) -> str:
        if value != enums.DeliveryIssueKind.INCIDENT:
            raise serializers.ValidationError(field_error("invalid_choice"))
        return value

    def validate_note(self, value: str) -> str:
        return clean_note(value, required=True)


class ResolveIssueSerializer(serializers.Serializer[dict[str, Any]]):
    note = serializers.CharField(allow_blank=True, trim_whitespace=True)

    def validate_note(self, value: str) -> str:
        return clean_note(value, required=True)


class SuspensionSerializer(serializers.Serializer[Any]):
    """The courier-dispatch switch: ``suspended`` is in effect now (``until`` passed → ``false``)."""

    suspended = serializers.BooleanField()
    until = serializers.DateTimeField(allow_null=True)
    note = serializers.CharField(allow_blank=True)
    updated_at = serializers.DateTimeField()


class SuspensionBodySerializer(serializers.Serializer[dict[str, Any]]):
    """``POST delivery/suspension/``: ``until`` (future, optional: until lifted) only while suspending."""

    suspended = serializers.BooleanField()
    until = AwareDateTimeField(required=False, allow_null=True, default=None)
    note = serializers.CharField(required=False, allow_blank=True, default="", trim_whitespace=True)

    def validate_note(self, value: str) -> str:
        if len(value) > SUSPENSION_NOTE_MAX_LENGTH:
            raise serializers.ValidationError(field_error("max_length", MESSAGE_SUSPENSION_NOTE_MAX))
        return value

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        until = attrs.get("until")
        if attrs["suspended"] and until is not None and until <= timeutils.now():
            raise serializers.ValidationError({"until": [field_error("in_past", MESSAGE_UNTIL_PAST)]})
        if not attrs["suspended"]:
            attrs["until"] = None
        return attrs


__all__ = [
    "ISSUE_NOTE_MAX_LENGTH",
    "ISSUE_NOTE_MIN_LENGTH",
    "MESSAGE_NOTE",
    "MESSAGE_NOTE_MAX",
    "MESSAGE_SUSPENSION_NOTE_MAX",
    "MESSAGE_UNTIL_PAST",
    "SUSPENSION_NOTE_MAX_LENGTH",
    "DeliveryIssueQuerySerializer",
    "DeliveryIssueSerializer",
    "OpenIssueSerializer",
    "ResolveIssueSerializer",
    "SuspensionBodySerializer",
    "SuspensionSerializer",
    "clean_note",
]
