"""The status push templates: the texts, the row and its update."""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from glowderma.core import enums
from glowderma.core.api.exceptions import field_error
from glowderma.core.api.serializers import ActorSerializer
from glowderma.notifications import services
from glowderma.notifications.api.admin.serializers.messages import MESSAGES, UNKNOWN_PLACEHOLDER
from glowderma.notifications.models import StatusTemplate


class TemplateTextsSerializer(serializers.Serializer[dict[str, str]]):
    uz = serializers.CharField(min_length=5, max_length=180)
    ru = serializers.CharField(max_length=180, allow_blank=True)
    en = serializers.CharField(max_length=180, allow_blank=True)

    def _placeholders(self, value: str) -> str:
        if UNKNOWN_PLACEHOLDER.search(value):
            raise serializers.ValidationError(
                [field_error("unknown_placeholder", MESSAGES["unknown_placeholder"])]
            )
        return value

    def validate_uz(self, value: str) -> str:
        return self._placeholders(value)

    def validate_ru(self, value: str) -> str:
        return self._placeholders(value)

    def validate_en(self, value: str) -> str:
        return self._placeholders(value)


class StatusTemplateSerializer(serializers.Serializer[StatusTemplate]):
    status = serializers.ChoiceField(choices=enums.TemplateStatus.values)
    texts = serializers.SerializerMethodField()
    updated_at = serializers.DateTimeField()
    updated_by = ActorSerializer(allow_null=True)

    @extend_schema_field(TemplateTextsSerializer())
    def get_texts(self, obj: StatusTemplate) -> dict[str, str]:
        return services.template_texts(obj)


class TemplateUpdateSerializer(serializers.Serializer[dict[str, Any]]):
    texts = TemplateTextsSerializer()


__all__ = [
    "StatusTemplateSerializer",
    "TemplateTextsSerializer",
    "TemplateUpdateSerializer",
]
