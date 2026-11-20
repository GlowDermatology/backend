"""Admin serializers of `assistant` (contract §2.19 AI assistant)."""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from glowderma.assistant import services
from glowderma.assistant.models import GenerationJob
from glowderma.catalog.api.admin.serializers import ProductSerializer
from glowderma.core import enums
from glowderma.core.api.exceptions import field_error
from glowderma.core.api.fields import BoundedListField
from glowderma.core.api.language import admin_language
from glowderma.core.api.serializers import ActorSerializer
from glowderma.core.uzsource import tr

MAX_KEYS = 120  # 3 languages x (5 texts + 2 SEO + 8 images' alt) = 45; generous
MAX_ITEMS = 120


class AssistantStatusSerializer(serializers.Serializer[Any]):
    enabled = serializers.BooleanField(help_text="The editor shows the AI assistant button.")
    max_images = serializers.IntegerField(help_text="Photos sent with a `photos` request (at most).")


class AssistantJobCreateSerializer(serializers.Serializer[dict[str, Any]]):
    mode = serializers.ChoiceField(
        choices=enums.AssistantMode.values,
        default=enums.AssistantMode.FILL_MISSING,
        help_text="`test` is refused (ops console only).",
    )
    languages = BoundedListField(
        child=serializers.ChoiceField(choices=enums.ContentLanguage.values),
        max_items=3,
        required=False,
        help_text="Target languages; omitted or empty = all three.",
    )
    keys = BoundedListField(
        child=serializers.CharField(max_length=64),
        max_items=MAX_KEYS,
        required=False,
        help_text="Snapshot keys (`texts.<lang>.<field>`, `seo.<lang>.<title|description>`, "
        "`images.<id>.alt.<lang>`): narrows the automatic choice; required for `improve`.",
    )
    goal = serializers.ChoiceField(
        choices=enums.AssistantImproveGoal.values, default=enums.AssistantImproveGoal.PROFESSIONAL
    )
    image_ids = BoundedListField(child=serializers.IntegerField(min_value=1), max_items=8, required=False)
    draft = serializers.DictField(
        child=serializers.CharField(allow_blank=True, trim_whitespace=False, max_length=4000),
        required=False,
        help_text=(
            'The admin\'s current (unsaved) form values: assistant key → value (`""` allowed), at most '
            f"{services.MAX_DRAFT_ENTRIES} entries. An image key must name an image of this product and each "
            "value fits its key's limit. A key's working value is its draft value when sent, else the saved "
            "one: the targets, each suggestion's `current` and the facts the model sees use working values."
        ),
    )

    def validate_draft(self, value: dict[str, str]) -> dict[str, str]:
        if len(value) > services.MAX_DRAFT_ENTRIES:
            raise serializers.ValidationError(field_error("max_items", limit=services.MAX_DRAFT_ENTRIES))
        return value

    def validate_languages(self, value: list[str]) -> list[str]:
        chosen = set(value)
        return [language for language in enums.ContentLanguage.values if language in chosen]


class AssistantSuggestionSerializer(serializers.Serializer[Any]):
    key = serializers.CharField()
    current = serializers.CharField(allow_blank=True)
    suggested = serializers.CharField()
    warnings = serializers.ListField(
        child=serializers.CharField(),
        help_text="`unsupported_claim`, `inci_mismatch`, `confirm_packaging`, `seo_title_long`, "
        "`seo_description_long`, `unverified_number` (a small count the data does not state).",
    )
    origin = serializers.CharField(help_text="`ai`, or `image` (read from the photos).")
    checked = serializers.BooleanField(help_text="Pre-selected in the review (photo suggestions never are).")
    max_length = serializers.IntegerField()


class AssistantJobResultSerializer(serializers.Serializer[Any]):
    suggestions = AssistantSuggestionSerializer(many=True)
    warnings = serializers.ListField(
        child=serializers.DictField(),
        help_text=(
            "Job-level notes by `code`: `rejected` {key, reason} (reason `too_long`, `unsupported_number`, "
            "`unsupported_spf`, `empty`, `pos_label`), `skipped` {key, reason} (the model could not write "
            "the key truthfully; short English reason), `unchanged` {key} (the suggestion equals the current "
            "value), `dropped` {count}, `extracted` {brand, volume} (photos)."
        ),
    )


class AssistantJobErrorSerializer(serializers.Serializer[Any]):
    code = serializers.CharField()
    message = serializers.CharField()


class AssistantJobSerializer(serializers.Serializer[GenerationJob]):
    """The poller-compatible job: ``state`` / ``error`` like ``Job`` (§2.14), plus the review fields."""

    id = serializers.IntegerField()
    product_id = serializers.IntegerField(allow_null=True)
    mode = serializers.ChoiceField(choices=enums.AssistantMode.values)
    languages = serializers.ListField(child=serializers.CharField())
    keys = serializers.ListField(child=serializers.CharField(), source="fields")
    goal = serializers.CharField(allow_blank=True)
    image_ids = serializers.ListField(child=serializers.IntegerField())
    state = serializers.ChoiceField(choices=enums.JobState.values)
    outcome = serializers.ChoiceField(choices=enums.AssistantJobOutcome.values)
    progress = serializers.SerializerMethodField()
    result = serializers.SerializerMethodField()
    error = serializers.SerializerMethodField()
    applied_keys = serializers.ListField(child=serializers.CharField())
    created_at = serializers.DateTimeField()
    started_at = serializers.DateTimeField(allow_null=True)
    finished_at = serializers.DateTimeField(allow_null=True)
    created_by = ActorSerializer()

    @extend_schema_field(serializers.JSONField(allow_null=True, help_text="Always null (no progress steps)."))
    def get_progress(self, obj: GenerationJob) -> None:
        return None

    @extend_schema_field(AssistantJobResultSerializer(allow_null=True))
    def get_result(self, obj: GenerationJob) -> dict[str, Any] | None:
        if obj.state != enums.JobState.SUCCEEDED:
            return None
        return {"suggestions": obj.suggestions, "warnings": obj.warnings}

    @extend_schema_field(AssistantJobErrorSerializer(allow_null=True))
    def get_error(self, obj: GenerationJob) -> dict[str, str] | None:
        if obj.state != enums.JobState.FAILED:
            return None
        request = self.context.get("request")
        message = services.error_message(obj.error_code)
        return {
            "code": obj.error_code,
            "message": tr(message, admin_language(request)) if request else message,
        }


class AssistantStartSerializer(serializers.Serializer[Any]):
    nothing_to_fill = serializers.BooleanField()
    job = AssistantJobSerializer(allow_null=True)


class AssistantApplyItemSerializer(serializers.Serializer[dict[str, Any]]):
    key = serializers.CharField(max_length=64)
    value = serializers.CharField(allow_blank=True, trim_whitespace=False, max_length=4000)


class AssistantApplySerializer(serializers.Serializer[dict[str, Any]]):
    items = BoundedListField(child=AssistantApplyItemSerializer(), max_items=MAX_ITEMS, allow_empty=False)


class AssistantAcceptSerializer(serializers.Serializer[dict[str, Any]]):
    keys = BoundedListField(
        child=serializers.CharField(max_length=64),
        max_items=MAX_ITEMS,
        allow_empty=True,
        help_text="The suggestion keys the person took into the form ([] = none).",
    )


class AssistantAcceptResultSerializer(serializers.Serializer[Any]):
    job = AssistantJobSerializer()


class AssistantApplyResultSerializer(serializers.Serializer[Any]):
    job = AssistantJobSerializer()
    applied = serializers.ListField(child=serializers.CharField())
    conflicts = serializers.ListField(child=serializers.CharField())
    product = ProductSerializer()
