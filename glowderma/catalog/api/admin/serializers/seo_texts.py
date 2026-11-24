"""The SEO texts of products and categories: the translated title and description, the issues and the
status."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from glowderma.catalog import services
from glowderma.core import enums


class SeoTextSerializer(serializers.Serializer[dict[str, str]]):
    """One language of ``seo`` (B-W1): the page ``title`` (≤120) and meta ``description`` (≤300); ``""`` =
    the website builds one from the name and description."""

    title = serializers.CharField(
        max_length=services.SEO_MAX_LENGTHS["title"], allow_blank=True, required=False
    )
    description = serializers.CharField(
        max_length=services.SEO_MAX_LENGTHS["description"], allow_blank=True, required=False
    )


class SeoTextsSerializer(serializers.Serializer[dict[str, Any]]):
    """Language-first ``seo`` ``{uz: {title, description}, ru: …, en: …}``; every language and leaf optional
    on write (merged leaf by leaf, like ``texts``)."""

    uz = SeoTextSerializer(required=False)
    ru = SeoTextSerializer(required=False)
    en = SeoTextSerializer(required=False)


class SeoIssueSerializer(serializers.Serializer[dict[str, Any]]):
    code = serializers.ChoiceField(choices=enums.SeoIssueCode.values)
    severity = serializers.ChoiceField(choices=enums.SeoSeverity.values)
    language = serializers.ChoiceField(choices=enums.ContentLanguage.values, allow_null=True)
    message = serializers.CharField()


class SeoStatusSerializer(serializers.Serializer[dict[str, Any]]):
    """«SEO holati» (B-W1): ``ok`` = no error and no warning; ``issues`` errors first, then warnings, then
    hints (``info``: a fallback is used)."""

    ok = serializers.BooleanField()
    issues = SeoIssueSerializer(many=True)


def seo_texts(obj: Any) -> dict[str, dict[str, str]]:
    return {
        language: {
            "title": getattr(obj, f"seo_title_{language}") or "",
            "description": getattr(obj, f"seo_description_{language}") or "",
        }
        for language in services.LANGUAGES
    }


__all__ = [
    "SeoIssueSerializer",
    "SeoStatusSerializer",
    "SeoTextSerializer",
    "SeoTextsSerializer",
    "seo_texts",
]
