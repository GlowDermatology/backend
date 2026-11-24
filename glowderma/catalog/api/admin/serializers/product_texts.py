"""The translated product texts (name, descriptions, ingredients, usage) and their writes."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from glowderma.catalog import services
from glowderma.catalog.api.admin.serializers.fields import UZ_NAME_MIN_LENGTH
from glowderma.catalog.models import Product
from glowderma.core.api.exceptions import field_error
from glowderma.core.uzsource import N_

TEXT_LIMITS = services.TEXT_MAX_LENGTHS


class ProductTextSerializer(serializers.Serializer[dict[str, str]]):
    """One language of ``texts``: ``{name, short_description, description, ingredients, usage}``."""

    name = serializers.CharField(max_length=TEXT_LIMITS["name"], allow_blank=True, required=False)
    short_description = serializers.CharField(
        max_length=TEXT_LIMITS["short_description"], allow_blank=True, required=False
    )
    description = serializers.CharField(
        max_length=TEXT_LIMITS["description"], allow_blank=True, required=False
    )
    ingredients = serializers.CharField(
        max_length=TEXT_LIMITS["ingredients"], allow_blank=True, required=False
    )
    usage = serializers.CharField(max_length=TEXT_LIMITS["usage"], allow_blank=True, required=False)


class ProductUzTextSerializer(ProductTextSerializer):
    """The Uzbek texts: ``name`` is mandatory on create and, whenever sent, trimmed 2..120 characters."""

    name = serializers.CharField(max_length=TEXT_LIMITS["name"], allow_blank=True)

    def validate_name(self, value: str) -> str:
        if len(value) < UZ_NAME_MIN_LENGTH:
            raise serializers.ValidationError(
                [field_error("min_length", N_("O‘zbekcha nomni kiriting (kamida 2 belgi)."))]
            )
        return value


class ProductTextsSerializer(serializers.Serializer[dict[str, Any]]):
    """Language-first product texts ``{uz: {...}, ru: {...}, en: {...}}`` (AGENTS.md: not ``i18n_text``)."""

    uz = ProductUzTextSerializer()
    ru = ProductTextSerializer(required=False)
    en = ProductTextSerializer(required=False)


class ProductUzTextPatchSerializer(ProductUzTextSerializer):
    """PATCH leaf merge: every Uzbek leaf is optional; a sent ``name`` keeps the 2..120 rule."""

    name = serializers.CharField(max_length=TEXT_LIMITS["name"], allow_blank=True, required=False)


class ProductTextsPatchSerializer(ProductTextsSerializer):
    """``texts`` of ``PATCH products/{id}/``: any subset of languages and leaves (§2.3 leaf merge)."""

    uz = ProductUzTextPatchSerializer(required=False)


def product_texts(product: Product) -> dict[str, dict[str, str]]:
    return {
        language: {field: getattr(product, f"{field}_{language}") or "" for field in services.TEXT_FIELDS}
        for language in services.LANGUAGES
    }


__all__ = [
    "TEXT_LIMITS",
    "ProductTextSerializer",
    "ProductTextsPatchSerializer",
    "ProductTextsSerializer",
    "ProductUzTextPatchSerializer",
    "ProductUzTextSerializer",
    "product_texts",
]
