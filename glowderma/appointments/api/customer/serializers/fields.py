"""What the customer ``appointments`` serializers share: the translated field, the language, the photo URL,
the coordinate and the any-offset datetime."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from rest_framework import serializers

from glowderma.core import i18n
from glowderma.core.media import absolute_media_url
from glowderma.professionals.models import Professional


def pick(obj: Any, prefix: str, lang: str) -> str:
    """``<prefix>_<lang>`` → ``<prefix>_uz`` → ``""`` (customer contract §1.4)."""
    return str(getattr(obj, f"{prefix}_{lang}", "") or getattr(obj, f"{prefix}_uz", "") or "")


def serializer_language(serializer: serializers.Serializer[Any]) -> str:
    return i18n.normalize_language(serializer.context.get("lang"))


def photo_url(professional: Professional) -> str | None:
    return absolute_media_url(professional.photo.name or "") if professional.photo else None


def coordinate(value: Any) -> float | None:
    return None if value is None else round(float(value), 6)


class AnyOffsetDateTimeField(serializers.DateTimeField):
    """An ISO datetime kept as submitted: a naive value is refused later, at its place in the booking table
    (C§2.11 row "start aware" → ``invalid``)."""

    def enforce_timezone(self, value: datetime) -> datetime:
        return value


__all__ = [
    "AnyOffsetDateTimeField",
    "coordinate",
    "photo_url",
    "pick",
    "serializer_language",
]
