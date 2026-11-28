"""Admin serializers of the «Bosh sahifa» (admin contract §2.21): a landing section with its hand-picked
items and the automatic choice, and the ``PUT`` body that replaces the picks."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from glowderma.core import enums


class HomeSectionItemSerializer(serializers.Serializer[dict[str, Any]]):
    id = serializers.CharField(help_text="Product or specialist id as a string; the category slug.")
    name = serializers.CharField(help_text="The Uzbek name.")
    image_url = serializers.CharField(allow_null=True, help_text="400 px WebP variant; null = no picture.")
    issue = serializers.ChoiceField(
        choices=enums.HomePickIssue.values,
        allow_null=True,
        help_text="Why a pick does not show on the landing now (it is skipped); null = it shows.",
    )


class HomeSectionSerializer(serializers.Serializer[dict[str, Any]]):
    key = serializers.ChoiceField(choices=enums.HomeSectionKey.values)
    manual = serializers.BooleanField(help_text="Staff filled it by hand; false = automatic.")
    limit = serializers.IntegerField(help_text="The most picks the section takes.")
    picks = HomeSectionItemSerializer(many=True, help_text="The saved picks in order; empty when automatic.")
    automatic = HomeSectionItemSerializer(
        many=True, help_text="What the automatic rule shows now (the starting point of a hand-filled list)."
    )


class HomeSectionsSerializer(serializers.Serializer[dict[str, Any]]):
    results = HomeSectionSerializer(many=True)


class HomeSectionWriteSerializer(serializers.Serializer[dict[str, Any]]):
    ids = serializers.ListField(
        child=serializers.CharField(max_length=160),
        allow_empty=True,
        help_text=(
            "The picks in order (ids as strings, category slugs), at most the section's `limit`; "
            "[] = automatic again."
        ),
    )


__all__ = [
    "HomeSectionItemSerializer",
    "HomeSectionSerializer",
    "HomeSectionWriteSerializer",
    "HomeSectionsSerializer",
]
