"""Admin serializers of `assistant` (contract §2.19 AI assistant)."""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from glowderma.assistant import services
from glowderma.assistant.models import GenerationJob
from glowderma.assistant.scenes import services as scene_services
from glowderma.catalog.api.admin.serializers import (
    BannerSerializer,
    ProductImageSerializer,
    ProductSerializer,
)
from glowderma.core import enums
from glowderma.core.api.exceptions import field_error
from glowderma.core.api.fields import BoundedListField
from glowderma.core.api.language import admin_language
from glowderma.core.api.serializers import ActorSerializer
from glowderma.core.media import absolute_media_url
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


# --- product scenes (contract §2.20) ------------------------------------------------------------------------


class SceneStatusSerializer(serializers.Serializer[Any]):
    enabled = serializers.BooleanField(help_text="Scenes and screen fitting are switched on.")
    ai_enabled = serializers.BooleanField(
        help_text="AI scene templates may be used (studio ones need only `enabled`)."
    )
    max_occluded_pct = serializers.DecimalField(
        max_digits=5,
        decimal_places=2,
        help_text="Share of the product a hand may hide for an AI scene to fill.",
    )
    segmenter = serializers.ChoiceField(choices=enums.SceneSegmenter.values)


class SceneTemplateSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    key = serializers.CharField()
    kind = serializers.ChoiceField(choices=enums.SceneTemplateKind.values)
    name = serializers.SerializerMethodField(help_text="In the request's language (else Uzbek).")
    colours = serializers.SerializerMethodField(
        help_text="A studio template's colours (#rrggbb) for a swatch."
    )
    preview_url = serializers.SerializerMethodField(
        help_text="The card's 4:5 picture of the scene alone: an AI template's reference, a studio "
        "template's background. Until an AI template has a reference it is its older sample picture "
        "(with a sample product), or null."
    )
    names = serializers.SerializerMethodField(help_text="`{uz, ru, en}`: every language, for the editor.")
    text = serializers.SerializerMethodField(
        help_text="An AI template's scene in the words staff typed (else the English text the AI reads)."
    )
    reference_url = serializers.SerializerMethodField(
        help_text="An AI template's reference: a picture of the scene alone (uploaded or made from the text)."
    )
    reference_source = serializers.CharField(
        allow_blank=True, help_text="`upload`, `generated`, or empty (no reference)."
    )
    enabled = serializers.BooleanField()
    saved_by_staff = serializers.SerializerMethodField(help_text="Made in the admin panel.")

    def get_names(self, obj: Any) -> dict[str, str]:
        return {"uz": obj.name_uz, "ru": obj.name_ru, "en": obj.name_en}

    def get_text(self, obj: Any) -> str:
        return str(obj.prompt_original or obj.prompt or "")

    @extend_schema_field(serializers.URLField(allow_null=True))
    def get_reference_url(self, obj: Any) -> str | None:
        url: str | None = obj.reference_url
        return url

    def get_saved_by_staff(self, obj: Any) -> bool:
        return obj.created_by_id is not None

    def get_name(self, obj: Any) -> str:
        request = self.context.get("request")
        language = admin_language(request) if request else "uz"
        return str(getattr(obj, f"name_{language}", "") or obj.name_uz)

    @extend_schema_field(serializers.URLField(allow_null=True))
    def get_preview_url(self, obj: Any) -> str | None:
        url: str | None = obj.preview_url
        return url

    @extend_schema_field(serializers.ListField(child=serializers.CharField()))
    def get_colours(self, obj: Any) -> list[str]:
        params = obj.params if isinstance(obj.params, dict) else {}
        colours = [str(c) for c in params.get("colours") or []]
        if params.get("floor"):
            colours.append(str(params["floor"]))
        return colours


class SceneTemplatesSerializer(serializers.Serializer[Any]):
    results = SceneTemplateSerializer(many=True)


class SceneTemplateNamesSerializer(serializers.Serializer[dict[str, Any]]):
    uz = serializers.CharField(max_length=60, allow_blank=True)
    ru = serializers.CharField(max_length=60, allow_blank=True, required=False, default="")
    en = serializers.CharField(max_length=60, allow_blank=True, required=False, default="")


class SceneTemplateWriteSerializer(serializers.Serializer[dict[str, Any]]):
    """`POST scenes/templates/` (all of it) and `PATCH scenes/templates/{id}/` (`partial`): JSON or multipart
    (dotted names: `name.uz`, …; the file part `reference`)."""

    name = SceneTemplateNamesSerializer(required=False)
    text = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=400,
        help_text="The scene in staff's words, any language (translated to English and screened).",
    )
    reference = serializers.ImageField(
        required=False,
        help_text="A picture of the scene alone (JPG, PNG, WebP; ≤ 5 MB): screened (no product, person or "
        "text) and described by the AI on the worker (the save is then a job).",
    )
    remove_reference = serializers.BooleanField(required=False, default=False)
    enabled = serializers.BooleanField(required=False)


# a product photo's scene jobs (a template's picture and save, and a banner image have their own endpoints)
STAFF_SCENE_KINDS = [
    kind
    for kind in enums.SceneJobKind.values
    if kind not in (enums.SceneJobKind.PREVIEW, enums.SceneJobKind.BANNER, enums.SceneJobKind.TEMPLATE)
]


class SceneJobCreateSerializer(serializers.Serializer[dict[str, Any]]):
    kind = serializers.ChoiceField(choices=STAFF_SCENE_KINDS)
    template_id = serializers.IntegerField(
        required=False, allow_null=True, help_text="Required unless `fit` or `custom_prompt`."
    )
    custom_prompt = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=400,
        help_text="`ai_scene` only, instead of `template_id`: the scene in staff's own words, any language "
        "(translated to English and screened by the worker).",
    )


class SceneTemplateSaveSerializer(serializers.Serializer[dict[str, Any]]):
    name = serializers.CharField(max_length=60, help_text="The card's name (all languages).")


class SceneCropsSerializer(serializers.Serializer[Any]):
    portrait = serializers.ListField(child=serializers.FloatField(), help_text="[x, y, w, h] 0-1 (4:5).")
    square = serializers.ListField(child=serializers.FloatField(), help_text="[x, y, w, h] 0-1 (1:1).")


class SceneCandidateSerializer(serializers.Serializer[Any]):
    url = serializers.URLField(allow_null=True, help_text="The new 4:5 image (null for `fit`).")
    overlay_url = serializers.URLField(
        allow_null=True, help_text="The same image with the parts drawn inside the product washed pink."
    )
    product_box = serializers.ListField(child=serializers.FloatField(), allow_null=True)
    crops = SceneCropsSerializer(
        help_text="Proposed shapes: of the candidate (scenes) or of the source (fit)."
    )
    generated_inside_pct = serializers.DecimalField(max_digits=5, decimal_places=2)
    occluded_pct = serializers.DecimalField(max_digits=5, decimal_places=2)


class SceneBannerProductSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    name = serializers.CharField()


class SceneBannerSerializer(serializers.Serializer[Any]):
    mode = serializers.ChoiceField(choices=["products", "scene"])
    products = SceneBannerProductSerializer(many=True, help_text="In the order staff picked them.")
    failed_product = SceneBannerProductSerializer(
        allow_null=True,
        help_text="The product whose photo could not be used (`banner_product_photo_unusable`).",
    )
    web_crop = serializers.ListField(
        child=serializers.FloatField(),
        help_text="[x, y, w, h] 0-1: the website's 2:1 window of the 2.15:1 image (the app shows all of it).",
    )
    accepted_banner_id = serializers.IntegerField(allow_null=True)


class SceneJobSerializer(serializers.Serializer[Any]):
    """The poller-compatible scene job: ``state`` / ``error`` like ``Job`` (§2.14) and the candidate."""

    id = serializers.IntegerField()
    product_id = serializers.IntegerField(allow_null=True)
    source_image_id = serializers.IntegerField(allow_null=True)
    kind = serializers.ChoiceField(choices=enums.SceneJobKind.values)
    template = serializers.SerializerMethodField()
    state = serializers.ChoiceField(choices=enums.JobState.values)
    outcome = serializers.ChoiceField(choices=enums.SceneJobOutcome.values)
    progress = serializers.SerializerMethodField()
    result = serializers.SerializerMethodField()
    error = serializers.SerializerMethodField()
    warnings = serializers.ListField(child=serializers.CharField())
    accepted_image_id = serializers.IntegerField(allow_null=True)
    banner = serializers.SerializerMethodField()
    created_at = serializers.DateTimeField()
    finished_at = serializers.DateTimeField(allow_null=True)
    expires_at = serializers.DateTimeField(allow_null=True)
    created_by = ActorSerializer()

    @extend_schema_field(
        serializers.JSONField(
            allow_null=True,
            help_text="`{key, name, custom_text, saved_template_id}`: `key` is `custom` for a scene staff "
            "typed (`custom_text`, null otherwise); `saved_template_id` once it was saved as a template.",
        )
    )
    def get_template(self, obj: Any) -> dict[str, Any] | None:
        snapshot = obj.template_snapshot or {}
        if not snapshot:
            return None
        request = self.context.get("request")
        language = admin_language(request) if request else "uz"
        names = snapshot.get("name") or {}
        return {
            "key": snapshot.get("key"),
            "name": names.get(language) or names.get("uz") or "",
            "custom_text": snapshot.get("custom_text") or None,
            "saved_template_id": snapshot.get("saved_template_id"),
        }

    @extend_schema_field(SceneBannerSerializer(allow_null=True))
    def get_banner(self, obj: Any) -> dict[str, Any] | None:
        if obj.kind != enums.SceneJobKind.BANNER:
            return None
        inputs = obj.inputs or {}
        products = [
            {"id": item.get("id"), "name": item.get("name") or ""} for item in inputs.get("products") or []
        ]
        failed_id = inputs.get("failed_product_id")
        failed = next((item for item in products if item["id"] == failed_id), None)
        crops = obj.crops or {}
        return {
            "mode": inputs.get("mode") or "scene",
            "products": products,
            "failed_product": failed,
            "web_crop": crops.get("2:1") or [],
            "accepted_banner_id": obj.accepted_banner_id,
        }

    @extend_schema_field(serializers.JSONField(allow_null=True, help_text="Always null (no progress steps)."))
    def get_progress(self, obj: Any) -> None:
        return None

    @extend_schema_field(SceneCandidateSerializer(allow_null=True))
    def get_result(self, obj: Any) -> dict[str, Any] | None:
        if obj.state != enums.JobState.SUCCEEDED or obj.outcome == enums.SceneJobOutcome.EXPIRED:
            return None
        if (
            obj.kind == enums.SceneJobKind.TEMPLATE
        ):  # its result is the template (`template.saved_template_id`)
            return None
        crops = obj.crops or {}
        return {
            "url": absolute_media_url(str(obj.candidate.name)) if obj.candidate else None,
            "overlay_url": absolute_media_url(str(obj.overlay.name)) if obj.overlay else None,
            "product_box": obj.product_box,
            "crops": {"portrait": crops.get("4:5") or [], "square": crops.get("1:1") or []},
            "generated_inside_pct": obj.generated_inside_pct,
            "occluded_pct": obj.occluded_pct,
        }

    @extend_schema_field(AssistantJobErrorSerializer(allow_null=True))
    def get_error(self, obj: Any) -> dict[str, str] | None:
        if obj.state != enums.JobState.FAILED:
            return None
        request = self.context.get("request")
        message = scene_services.message_of(obj.error_code)
        return {
            "code": obj.error_code,
            "message": tr(message, admin_language(request)) if request else message,
        }


class BannerImageCreateSerializer(serializers.Serializer[dict[str, Any]]):
    mode = serializers.ChoiceField(
        choices=["products", "scene"],
        help_text="`products`: 1-3 catalog products in a scene (each from its main photo); `scene`: a scene "
        "alone.",
    )
    product_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        required=False,
        default=list,
        max_length=3,
        help_text="`products` only: 1-3 product ids; the first is the main one (the tallest).",
    )
    template_id = serializers.IntegerField(
        required=False, allow_null=True, help_text="An AI template (required unless `custom_prompt`)."
    )
    custom_prompt = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=400,
        help_text="The scene in staff's own words, any language, instead of `template_id`.",
    )


class BannerImageUseSerializer(serializers.Serializer[dict[str, Any]]):
    banner_id = serializers.IntegerField(
        required=False, allow_null=True, help_text="An existing banner; null or absent: a new banner."
    )
    banner = serializers.JSONField(
        help_text="The banner's fields as in `POST banners/` (new) or `PATCH banners/{id}/` (existing), "
        "without `image`."
    )


class BannerImageUseResponseSerializer(serializers.Serializer[Any]):
    job = SceneJobSerializer()
    banner = BannerSerializer()


class SceneAcceptSerializer(serializers.Serializer[dict[str, Any]]):
    make_main = serializers.BooleanField(default=False, help_text="Put the new image first (scenes only).")
    acknowledge_generated = serializers.BooleanField(
        default=False,
        help_text="Required true when `generated_inside_pct` > 0: staff checked the pink parts.",
    )


class SceneAcceptResponseSerializer(serializers.Serializer[Any]):
    job = SceneJobSerializer()
    image = ProductImageSerializer()
