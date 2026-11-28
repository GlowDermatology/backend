"""Product scene jobs: start, run (worker), accept, discard, reap and expire.

A job works on one saved product image (``source_image``) and is one of:

* ``studio`` / ``ai_scene`` — a new background around the product (``pipeline.studio`` / ``ai_scene``). The
  candidate (a 4:5 WebP) waits on the job until staff accept it as a **new** gallery image (the original
  stays) or discard it; unreviewed candidates expire after ``candidate_ttl_hours``.
* ``fit`` — only the product's box (one cheap analyst call): it proposes 4:5 and square crops framed on the
  product, which accepting writes as the source image's renditions.

Every job is announced on the outbox (``assistant.scene_queued``) and run by the Celery task
``assistant.run_scene`` with ids only; ``claim`` makes a duplicate delivery a no-op. Budgets and the per-user
daily limit are the scenes' own (``SceneSettings``), apart from the text assistant's.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal
from functools import cache
from io import BytesIO
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from PIL import Image
from rest_framework import exceptions as drf_exceptions
from rest_framework.exceptions import NotFound, ValidationError

from glowderma.assistant import credentials, topics
from glowderma.assistant.models import (
    ACTIVE_SCENE_STATES,
    SETTINGS_PK,
    ProviderConfig,
    SceneJob,
    SceneSettings,
    SceneTemplate,
)
from glowderma.assistant.scenes import banner, pipeline, prompts
from glowderma.assistant.scenes.templates import Template
from glowderma.catalog import services as catalog_services
from glowderma.catalog.models import ProductImage
from glowderma.core import audit, compose, enums, outbox, renditions, timeutils
from glowderma.core.api.exceptions import MESSAGES, Conflict, field_error
from glowderma.core.uzsource import N_
from glowderma.integrations.llm import registry as llm_registry
from glowderma.integrations.llm.ports import LLMError
from glowderma.integrations.secret import Secret
from glowderma.integrations.segmentation import registry as segmentation_registry
from glowderma.integrations.vault.ports import VaultError

logger = logging.getLogger(__name__)

STALE_AFTER = timedelta(minutes=9)  # longer than the task's hard limit (tasks.SCENE_TIME_LIMIT)
# a queued job may wait behind another one (one scene at a time): it is lost only after this
QUEUED_STALE_AFTER = timedelta(minutes=30)
FAKE_KEY = "fake-key"
SAMPLE_PRODUCT = Path(__file__).with_name("assets") / "sample-product.webp"  # the owner's own product
PREVIEW_WIDTH = 600  # a studio template's card picture (4:5)
REFERENCE_SIZE = pipeline.REFERENCE_SIZE
CUSTOM_KEY = "custom"
CUSTOM_NAMES = {"uz": "O‘z sahnasi", "ru": "Своя сцена", "en": "Custom scene"}
TEMPLATE_NAME_MAX = 60
COST_QUANT = Decimal("0.000001")
SERVER_ERROR = "server_error"
WORKER_LOST = "worker_lost"

ERROR_MESSAGES: dict[str, str] = {
    "scenes_disabled": N_("Sahnalar o‘chirilgan."),
    "scene_ai_disabled": N_("AI sahnalar hali yoqilmagan: studiya shablonidan foydalaning."),
    "scene_template_unavailable": N_("Bu shablon mavjud emas yoki o‘chirilgan."),
    pipeline.NO_PRODUCT: N_("Rasmda mahsulot topilmadi."),
    pipeline.MASK_UNUSABLE: N_("Mahsulotni fondan aniq ajratib bo‘lmadi. Boshqa rasm bilan urinib ko‘ring."),
    pipeline.HAND_COVERS_PRODUCT: N_("Qo‘l mahsulotning katta qismini yopib turibdi: rasmni qayta oling."),
    pipeline.OCCLUSION_NEEDS_AI: N_(
        "Qo‘l mahsulotning bir qismini yopib turibdi: studiya shabloni buni to‘ldira olmaydi. AI sahnasini "
        "tanlang yoki rasmni qayta oling."
    ),
    pipeline.OCCLUSION_NOT_FILLABLE: N_(
        "Qo‘l mahsulotning bir qismini yopib turibdi: Bria sahnasi faqat fonni chizadi va yopilgan joyni "
        "to‘ldira olmaydi. Rasmni qayta oling."
    ),
    pipeline.CUSTOM_REFUSED: N_(
        "Bu tavsif bo‘yicha sahna yaratib bo‘lmaydi: unda odamlar, qo‘llar, yozuv yoki brend bo‘lmasligi "
        "kerak. Faqat fon va atrofni tasvirlang."
    ),
    "scene_not_custom": N_("Faqat o‘zingiz yozgan, tayyor sahnani shablon sifatida saqlash mumkin."),
    "scene_template_exists": N_("Bu sahna allaqachon shablon sifatida saqlangan."),
    "scene_template_name": N_("Shablon nomini yozing."),
    "scene_custom_text": N_("Sahnani tasvirlang (ko‘pi bilan 400 belgi)."),
    pipeline.PRODUCT_CUT_OFF: N_(
        "Mahsulot rasmga to‘liq sig‘magan: rasmni mahsulot to‘liq ko‘rinadigan qilib oling."
    ),
    pipeline.PRODUCT_DRIFT: N_("AI mahsulotni o‘zgartirib yubordi, natija rad etildi. Qayta urinib ko‘ring."),
    pipeline.RESULT_REJECTED: N_("Natijada begona narsalar qoldi (qo‘l, ofis, yozuv). Qayta urinib ko‘ring."),
    pipeline.RULE_BROKEN: MESSAGES["server_error"],
    "provider_unavailable": MESSAGES["provider_unavailable"],
    "ai_not_configured": N_("AI xizmati sozlanmagan. Administratorga murojaat qiling."),
    "ai_refused": N_("AI bu rasmni qayta ishlamadi. Boshqa rasm bilan urinib ko‘ring."),
    "ai_bad_output": N_("AI javobini tushunib bo‘lmadi. Qayta urinib ko‘ring."),
    "vault_unavailable": N_("AI xizmati vaqtincha ishlamayapti. Birozdan so‘ng qayta urinib ko‘ring."),
    banner.PRODUCT_PHOTO_MISSING: N_("Tanlangan mahsulotlardan birida rasm yo‘q."),
    banner.PRODUCT_PHOTO_UNUSABLE: N_(
        "Mahsulot rasmidan foydalanib bo‘lmadi (mahsulot to‘liq ko‘rinmaydi yoki uni qo‘l yopib turibdi). "
        "Boshqa mahsulotni tanlang yoki uning asosiy rasmini almashtiring."
    ),
    "banner_products": N_("1 tadan 3 tagacha mahsulot tanlang."),
    "scene_source_changed": N_("Rasm o‘zgartirilgan yoki o‘chirilgan: qaytadan yarating."),
    WORKER_LOST: N_("Jarayon to‘xtab qoldi. Qayta urinib ko‘ring."),
    SERVER_ERROR: MESSAGES["server_error"],
}


def message_of(code: str) -> str:
    return ERROR_MESSAGES.get(code, MESSAGES["server_error"])


class ScenesDisabled(Conflict):
    default_code = "scenes_disabled"
    default_detail = ERROR_MESSAGES["scenes_disabled"]


class AiScenesDisabled(Conflict):
    default_code = "scene_ai_disabled"
    default_detail = ERROR_MESSAGES["scene_ai_disabled"]


class SceneBudgetExceeded(Conflict):
    default_code = "assistant_budget_exceeded"
    default_detail = N_("Sahnalar uchun ajratilgan byudjet tugadi. Administratorga murojaat qiling.")


class SceneDailyLimit(drf_exceptions.APIException):
    status_code = 429
    default_code = "scene_daily_limit"
    default_detail = N_("Bugungi sahna so‘rovlari chegarasiga yetdingiz. Ertaga qayta urinib ko‘ring.")


class SceneBusy(Exception):
    """Another scene job is running: at most one at a time, so a scene never holds both worker slots that
    orders, payments and messages share. The task retries shortly."""


class SceneNotReviewable(Conflict):
    default_code = "scene_job_not_ready"
    default_detail = N_("Bu natija hali tayyor emas yoki allaqachon ko‘rib chiqilgan.")


class SceneNotCustom(Conflict):
    default_code = "scene_not_custom"
    default_detail = ERROR_MESSAGES["scene_not_custom"]


class SceneTemplateExists(Conflict):
    default_code = "scene_template_exists"
    default_detail = ERROR_MESSAGES["scene_template_exists"]


class SceneSourceChanged(Conflict):
    default_code = "scene_source_changed"
    default_detail = N_("Asl rasm o‘zgargan yoki o‘chirilgan: natijani qayta yarating.")


# --- settings, limits ---------------------------------------------------------------------------------------


def get_settings() -> SceneSettings:
    row, _ = SceneSettings.objects.get_or_create(pk=SETTINGS_PK)
    return row


def status() -> dict[str, Any]:
    row = get_settings()
    return {
        "enabled": row.enabled,
        "ai_enabled": row.enabled and row.ai_enabled,
        "max_occluded_pct": row.max_occluded_pct,
        "segmenter": row.segmenter,
    }


def _spent(**bounds: Any) -> Decimal:
    total = SceneJob.objects.filter(**bounds).aggregate(total=Sum("cost_usd"))["total"]
    return Decimal(total or 0)


def spent_today() -> Decimal:
    start, end = timeutils.day_bounds(timeutils.local_date(timeutils.now()))
    return _spent(created_at__gte=start, created_at__lt=end)


def spent_this_month() -> Decimal:
    start, _ = timeutils.day_bounds(timeutils.month_start(timeutils.local_date(timeutils.now())))
    return _spent(created_at__gte=start)


def jobs_today(actor: Any) -> int:
    start, end = timeutils.day_bounds(timeutils.local_date(timeutils.now()))
    return SceneJob.objects.filter(created_by=actor, created_at__gte=start, created_at__lt=end).count()


def prices(row: SceneSettings) -> pipeline.Prices:
    return pipeline.Prices(
        text_input=row.price_text_input,
        text_output=row.price_text_output,
        image_input=row.price_image_input,
        image_text_output=row.price_image_text_output,
        image_output=row.price_image_output,
        bria_per_call=row.price_bria_call,
        bria_scene_per_call=row.price_bria_scene_call,
    )


# --- start --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class StartResult:
    job: SceneJob
    created: bool


def _active(image_id: int) -> SceneJob | None:
    return SceneJob.objects.filter(source_image_id=image_id, state__in=ACTIVE_SCENE_STATES).first()


def _template_snapshot(template: SceneTemplate) -> dict[str, Any]:
    return {
        "key": template.key,
        "kind": template.kind,
        "name": {"uz": template.name_uz, "ru": template.name_ru, "en": template.name_en},
        "params": template.params,
        "prompt": template.prompt,
        # the reference file as it was at the start (a replaced one is kept while a job still reads it)
        "reference": str(template.reference.name or ""),
        "reference_description": template.reference_description,
    }


def _custom_snapshot(text: str) -> dict[str, Any]:
    """A scene staff typed: translated to English by the worker (``prompt`` is filled then)."""
    return {
        "key": CUSTOM_KEY,
        "kind": enums.SceneTemplateKind.AI_SCENE,
        "name": dict(CUSTOM_NAMES),
        "params": {},
        "prompt": "",
        "custom_text": text,
    }


def start_job(
    product_id: int,
    image_id: int,
    *,
    kind: str,
    template_id: int | None,
    actor: Any,
    custom_prompt: str = "",
) -> StartResult:
    """Queue a scene / fit job on a saved image, or return the image's active job (``created=False``).

    An AI scene takes a template or ``custom_prompt`` (a scene staff typed, any language)."""
    row = get_settings()
    if not row.enabled:
        raise ScenesDisabled()
    image = ProductImage.objects.filter(pk=image_id, product_id=product_id).first()
    if image is None:
        raise NotFound
    if kind == enums.SceneJobKind.PREVIEW:  # the console's, never staff's
        raise ValidationError(
            {"kind": [field_error("invalid", ERROR_MESSAGES["scene_template_unavailable"])]}
        )
    template: SceneTemplate | None = None
    snapshot: dict[str, Any] = {}
    custom = " ".join(custom_prompt.split())
    if kind != enums.SceneJobKind.FIT and custom:
        if kind != enums.SceneJobKind.AI_SCENE or len(custom) > prompts.CUSTOM_MAX_CHARS:
            raise ValidationError(
                {"custom_prompt": [field_error("invalid", ERROR_MESSAGES["scene_custom_text"])]}
            )
        if not row.ai_enabled:
            raise AiScenesDisabled()
        snapshot = _custom_snapshot(custom)
    elif kind != enums.SceneJobKind.FIT:
        template = SceneTemplate.objects.filter(pk=template_id, enabled=True).first() if template_id else None
        if template is None or template.kind != kind:
            raise ValidationError(
                {"template_id": [field_error("invalid", ERROR_MESSAGES["scene_template_unavailable"])]}
            )
        if kind == enums.SceneJobKind.AI_SCENE and not row.ai_enabled:
            raise AiScenesDisabled()
        snapshot = _template_snapshot(template)
    active = _active(image.pk)
    if active is not None:
        return StartResult(job=active, created=False)
    if jobs_today(actor) >= row.per_user_daily_jobs:
        raise SceneDailyLimit()
    if spent_today() >= row.daily_budget_usd or spent_this_month() >= row.monthly_budget_usd:
        raise SceneBudgetExceeded()
    try:
        with transaction.atomic():
            job = SceneJob.objects.create(
                product_id=product_id,
                source_image=image,
                source_sha256=image.sha256,
                kind=kind,
                template=template,
                template_snapshot=snapshot,
                created_by=actor,
            )
            outbox.enqueue(
                topics.SCENE_QUEUED, {"job_id": job.pk}, dedupe_key=f"{topics.SCENE_QUEUED}:{job.pk}"
            )
    except IntegrityError:  # a concurrent start on the same image won the one-active-job slot
        active = _active(image.pk)
        if active is None:  # pragma: no cover - it finished between the two statements
            raise
        return StartResult(job=active, created=False)
    logger.info("scene job %s queued", job.pk)  # ids only: the row holds the rest
    return StartResult(job=job, created=True)


# --- run (worker) -------------------------------------------------------------------------------------------


def _is_stale(job: SceneJob, instant: Any) -> bool:
    reference = job.heartbeat_at or job.started_at or job.created_at
    return bool(reference < instant - STALE_AFTER)


def claim(job_id: int) -> SceneJob | None:
    with transaction.atomic():
        job = SceneJob.objects.select_for_update().filter(pk=job_id).first()
        if job is None:
            return None
        instant = timeutils.now()
        if job.state == enums.JobState.QUEUED:
            running = SceneJob.objects.filter(
                state=enums.JobState.RUNNING, heartbeat_at__gte=instant - STALE_AFTER
            ).exclude(pk=job.pk)
            if running.exists():
                raise SceneBusy()
            job.started_at = instant
        elif job.state != enums.JobState.RUNNING or not _is_stale(job, instant):
            return None
        job.state = enums.JobState.RUNNING
        job.heartbeat_at = instant
        job.attempts += 1
        job.save(update_fields=["state", "started_at", "heartbeat_at", "attempts", "updated_at"])
        return job


def _finish(job_id: int, **values: Any) -> SceneJob | None:
    with transaction.atomic():
        job = SceneJob.objects.select_for_update().filter(pk=job_id).first()
        if job is None or job.state != enums.JobState.RUNNING:
            return None
        instant = timeutils.now()
        for name, value in values.items():
            setattr(job, name, value)
        job.finished_at = instant
        job.heartbeat_at = instant
        job.save()
        return job


def _usage_dict(usage: pipeline.Usage | None) -> dict[str, int]:
    if usage is None:
        return {}
    return {
        "text_input": usage.text_input,
        "text_output": usage.text_output,
        "image_input": usage.image_input,
        "image_text_output": usage.image_text_output,
        "image_output": usage.image_output,
        "text_calls": usage.text_calls,
        "image_calls": usage.image_calls,
        "bria_calls": usage.bria_calls,
        "bria_scene_calls": usage.bria_scene_calls,
    }


def _cost(row: SceneSettings, usage: pipeline.Usage | None) -> Decimal:
    if usage is None:
        return Decimal(0)
    return usage.cost(prices(row)).quantize(COST_QUANT, rounding=ROUND_HALF_UP)


def fail(job: SceneJob, code: str, *, usage: pipeline.Usage | None = None) -> SceneJob | None:
    logger.warning("scene job %s failed: %s", job.pk, code)
    return _finish(
        job.pk,
        state=enums.JobState.FAILED,
        error_code=code[:40],
        usage=_usage_dict(usage),
        cost_usd=_cost(get_settings(), usage),
    )


def _key(provider: str) -> Secret:
    config = ProviderConfig.objects.filter(provider=provider).first()
    if config is None or config.active_version is None:
        if settings.AI_MODE == "fake":
            return Secret(FAKE_KEY)
        raise LLMError(f"no active {provider} key", code="ai_not_configured")
    key: Secret = credentials.resolve_active_key(config)
    return key


def _key_if_set(provider: str) -> Secret:
    """The provider's active key, or an empty one: the adapter refuses it (``ai_not_configured``) only when
    it is actually called, so a missing Bria key never stops a studio scene or a fit."""
    try:
        return _key(provider)
    except LLMError:
        return Secret("")


def providers(row: SceneSettings) -> pipeline.Providers:
    """Each provider with its own key: Gemini for the analyst and the QA check, the cutter's and the AI scene
    provider's keys as the console chose them (Bria's from «AI provayderlar → Bria»)."""
    gemini = _key(enums.AssistantProvider.GEMINI)
    bria_needed = enums.SceneImageProvider.BRIA in (row.segmenter, row.scene_provider)
    bria = _key_if_set(enums.AssistantProvider.BRIA) if bria_needed else Secret("")
    cutter_key = bria if row.segmenter == enums.SceneSegmenter.BRIA else gemini
    image_key = bria if row.scene_provider == enums.SceneImageProvider.BRIA else gemini
    return pipeline.Providers(
        segmenter=segmentation_registry.get_segmenter(
            row.segmenter, image_model=row.image_model, cutter_key=cutter_key, bria_model=row.bria_model
        ),
        image_model=llm_registry.get_image_model(row.scene_provider),
        content_model=llm_registry.get_content_model(),
        api_key=gemini,
        image_key=image_key,
    )


def config_of(row: SceneSettings) -> pipeline.Config:
    return pipeline.Config(
        segment_model=row.analyst_model,
        image_model=row.image_model,
        qa_model=row.analyst_model,
        image_size=row.image_size,
        max_attempts=row.max_attempts,
        max_occluded_pct=float(row.max_occluded_pct),
        qa_check=row.qa_check,
        style=row.style,
    )


def _reference_bytes(name: str) -> bytes | None:
    if not name:
        return None
    storage = SceneTemplate._meta.get_field("reference").storage
    try:
        with storage.open(name, "rb") as handle:
            return bytes(handle.read())
    except OSError:  # deleted meanwhile: the scene's words still hold
        logger.warning("scene reference %s is missing", name)
        return None


def template_of(snapshot: dict[str, Any], *, with_reference: bool = True) -> Template:
    return Template(
        key=str(snapshot.get("key") or ""),
        kind=snapshot.get("kind") or "studio",
        name=dict(snapshot.get("name") or {}),
        params=dict(snapshot.get("params") or {}),
        prompt=str(snapshot.get("prompt") or ""),
        reference=_reference_bytes(str(snapshot.get("reference") or "")) if with_reference else None,
        reference_description=str(snapshot.get("reference_description") or ""),
    )


def _norm_box(box: compose.Box, width: int, height: int) -> list[float]:
    return [
        round(box[0] / width, 5),
        round(box[1] / height, 5),
        round(box[2] / width, 5),
        round(box[3] / height, 5),
    ]


def _source_bytes(image: ProductImage) -> bytes:
    with image.file.storage.open(str(image.file.name), "rb") as handle:
        return bytes(handle.read())


def run(job_id: int) -> SceneJob | None:
    """The worker side: claim, run the pipeline, store the candidate (or the fit proposal)."""
    job = claim(job_id)
    if job is None:
        return None
    if job.kind == enums.SceneJobKind.PREVIEW:
        return _run_preview(job)
    if job.kind == enums.SceneJobKind.BANNER:
        from glowderma.assistant.scenes import banner_jobs  # it builds on this module

        return banner_jobs.run(job)
    image = ProductImage.objects.filter(pk=job.source_image_id).first() if job.source_image_id else None
    if image is None or image.sha256 != job.source_sha256:
        return fail(job, "scene_source_changed")
    row = get_settings()
    usage = pipeline.Usage()
    try:
        used = providers(row)
        photo = pipeline.load_photo(_source_bytes(image))
        config = config_of(row)
        if job.kind == enums.SceneJobKind.FIT:
            analysis = pipeline.analyse(photo, used, config, usage, for_scene=False)
            box = _norm_box(analysis.box, photo.width, photo.height)
            crops = {
                ratio: list(renditions.auto_crop(ratio, photo.width, photo.height, box))
                for ratio in renditions.RATIOS
            }
            return _finish(
                job.pk,
                state=enums.JobState.SUCCEEDED,
                product_box=box,
                crops=crops,
                warnings=analysis.warnings,
                usage=_usage_dict(usage),
                cost_usd=_cost(row, usage),
                expires_at=timeutils.now() + timedelta(hours=row.candidate_ttl_hours),
            )
        snapshot = dict(job.template_snapshot or {})
        if snapshot.get("custom_text") and not snapshot.get("prompt"):
            # translated once; kept on the job, so a retry and «save as template» reuse it
            snapshot["prompt"] = pipeline.custom_scene(str(snapshot["custom_text"]), used, config, usage)
            SceneJob.objects.filter(pk=job.pk).update(template_snapshot=snapshot)
        template = template_of(snapshot)
        analysis, result = pipeline.run(photo, template, used, config, usage)
    except pipeline.SceneError as exc:
        return fail(job, exc.code, usage=exc.usage or usage)
    except LLMError as exc:
        return fail(job, exc.code, usage=usage)
    except VaultError:
        return fail(job, "vault_unavailable", usage=usage)
    width, height = result.final.size
    square = result.square
    crops = {"4:5": [0.0, 0.0, 1.0, 1.0], "1:1": list(renditions_crop(square, width, height))}
    candidate = ContentFile(compose.encode(result.final, "WEBP", 92), name=f"{job.pk}-scene.webp")
    overlay = None
    if result.generated_inside is not None:
        overlay = ContentFile(
            compose.encode(compose.tint(result.final, result.generated_inside), "WEBP", 85),
            name=f"{job.pk}-overlay.webp",
        )
    with transaction.atomic():
        locked = SceneJob.objects.select_for_update().filter(pk=job.pk).first()
        if locked is None or locked.state != enums.JobState.RUNNING:
            return None
        locked.candidate.save(candidate.name or "scene.webp", candidate, save=False)
        if overlay is not None:
            locked.overlay.save(overlay.name or "overlay.webp", overlay, save=False)
        layout_box = result.product_box
        locked.product_box = _norm_box(layout_box, width, height)
        locked.crops = crops
        locked.warnings = result.warnings
        locked.occluded_pct = Decimal(str(analysis.occluded_pct)).quantize(Decimal("0.01"))
        locked.generated_inside_pct = Decimal(str(result.generated_inside_pct)).quantize(Decimal("0.01"))
        locked.usage = _usage_dict(usage)
        locked.cost_usd = _cost(row, usage)
        locked.state = enums.JobState.SUCCEEDED
        instant = timeutils.now()
        locked.finished_at = instant
        locked.heartbeat_at = instant
        locked.expires_at = instant + timedelta(hours=row.candidate_ttl_hours)
        locked.save()
        return locked


def renditions_crop(box: compose.Box, width: int, height: int) -> tuple[float, float, float, float]:
    """A pixel box of the master as a normalised ``[x, y, w, h]`` crop."""
    return (
        round(box[0] / width, 5),
        round(box[1] / height, 5),
        round((box[2] - box[0]) / width, 5),
        round((box[3] - box[1]) / height, 5),
    )


# --- template previews --------------------------------------------------------------------------------------


@cache
def _sample_bytes() -> bytes:
    return SAMPLE_PRODUCT.read_bytes()


def sample_product() -> Image.Image:
    with Image.open(BytesIO(_sample_bytes())) as source:
        source.load()
        return source.convert("RGBA")


def _preview_content(final: Image.Image, key: str) -> ContentFile[bytes]:
    picture = final.convert("RGB")
    if picture.width > PREVIEW_WIDTH:
        height = round(picture.height * PREVIEW_WIDTH / picture.width)
        picture = picture.resize((PREVIEW_WIDTH, height), Image.Resampling.LANCZOS)
    data = compose.encode(picture, "WEBP", 85)
    return ContentFile(data, name=f"{key}-{hashlib.sha256(data).hexdigest()[:12]}.webp")


def _store_preview(template: SceneTemplate, content: ContentFile[bytes] | None) -> None:
    """Replace (``None``: clear) a studio template's card picture; the old file goes once the transaction
    commits."""
    old = str(template.preview.name or "")
    if content is None:
        template.preview = ""
        template.preview_scene_only = False
    else:
        template.preview.save(content.name or "preview.webp", content, save=False)
        template.preview_scene_only = True
    template.save(update_fields=["preview", "preview_scene_only", "updated_at"])
    if old and old != str(template.preview.name or ""):
        _delete_later(template.preview.storage, old)


def reference_in_use(name: str) -> bool:
    """Whether a queued or running job still reads this reference file (its snapshot names it)."""
    return (
        bool(name)
        and SceneJob.objects.filter(state__in=ACTIVE_SCENE_STATES, template_snapshot__reference=name).exists()
    )


def store_reference(
    template: SceneTemplate,
    content: ContentFile[bytes] | None,
    *,
    source: str = "",
    description: str = "",
) -> None:
    """Replace (``None``: clear) an AI template's reference. The old file goes once the transaction commits,
    unless a queued job still reads it (it is then left behind: a small file, never a broken job). The old
    sample picture (``preview``, with the sample product) goes with it."""
    old = str(template.reference.name or "")
    if content is None:
        template.reference = ""
        template.reference_source = ""
        template.reference_description = ""
    else:
        template.reference.save(content.name or "reference.webp", content, save=False)
        template.reference_source = source
        template.reference_description = description
    stale_preview = str(template.preview.name or "") if content is not None else ""
    if stale_preview:
        template.preview = ""
        template.preview_scene_only = False
    template.save()
    if old and old != str(template.reference.name or "") and not reference_in_use(old):
        _delete_later(template.reference.storage, old)
    if stale_preview:
        _delete_later(template.preview.storage, stale_preview)


def _same_scene(template: SceneTemplate, snapshot: dict[str, Any]) -> bool:
    return (
        template.kind == snapshot.get("kind")
        and template.prompt == (snapshot.get("prompt") or "")
        and template.params == (snapshot.get("params") or {})
        and str(template.reference.name or "") == (snapshot.get("reference") or "")
    )


def render_studio_preview(template: SceneTemplate) -> None:
    """A studio template's card: its background alone (no product), composed on our server (no cost)."""
    background = compose.studio_background(REFERENCE_SIZE, dict(template.params or {}))
    _store_preview(template, _preview_content(background, template.key))


def ensure_studio_previews() -> None:
    """Studio cards drawn before references existed show the sample product: redraw them (idempotent: a
    background-only picture is left alone)."""
    for template in SceneTemplate.objects.filter(
        kind=enums.SceneTemplateKind.STUDIO, preview_scene_only=False
    ):
        with transaction.atomic():
            locked = SceneTemplate.objects.select_for_update().filter(pk=template.pk).first()
            if locked is not None and not locked.preview_scene_only:
                render_studio_preview(locked)


def template_changed(template: SceneTemplate) -> None:
    """After params or prompt changed: a studio card is redrawn at once; an AI template's reference made from
    the old text no longer shows it and is cleared (an uploaded one stays: staff chose that picture)."""
    if template.kind == enums.SceneTemplateKind.STUDIO:
        render_studio_preview(template)
    elif template.reference_source == enums.SceneReferenceSource.GENERATED:
        store_reference(template, None)
    elif template.preview and not template.reference:
        _store_preview(template, None)


class SceneTemplateNeedsText(Conflict):
    default_code = "scene_template_needs_text"
    default_detail = N_("Rasmni yaratish uchun shablonga sahna tavsifini yozing.")


def start_preview(template_id: int, *, actor: Any) -> SceneJob | None:
    """«Rasmini yaratish»: a studio card is drawn now (``None``); an AI template's reference is made from its
    text by a job (one pending per template: a second click returns it), within the scene budget."""
    row = get_settings()
    with transaction.atomic():
        template = SceneTemplate.objects.select_for_update().filter(pk=template_id).first()
        if template is None:
            raise NotFound
        if template.kind == enums.SceneTemplateKind.STUDIO:
            render_studio_preview(template)
            return None
        if not template.prompt:
            raise SceneTemplateNeedsText()
        pending = SceneJob.objects.filter(
            kind=enums.SceneJobKind.PREVIEW, template=template, state__in=ACTIVE_SCENE_STATES
        ).first()
        if pending is not None:
            return pending
        if spent_today() >= row.daily_budget_usd or spent_this_month() >= row.monthly_budget_usd:
            raise SceneBudgetExceeded()
        job = SceneJob.objects.create(
            kind=enums.SceneJobKind.PREVIEW,
            template=template,
            template_snapshot=_template_snapshot(template),
            source_sha256="",
            created_by=actor,
        )
        outbox.enqueue(topics.SCENE_QUEUED, {"job_id": job.pk}, dedupe_key=f"{topics.SCENE_QUEUED}:{job.pk}")
    logger.info("scene reference job %s queued", job.pk)
    return job


def start_missing_previews(*, actor: Any) -> tuple[int, int]:
    """The console's «Yo‘q namunalarni yaratish»: (studio cards drawn, AI reference jobs queued). An AI
    template without a reference (its card still shows the sample product, or nothing) gets one."""
    drawn = queued = 0
    studio = SceneTemplate.objects.filter(kind=enums.SceneTemplateKind.STUDIO).filter(
        Q(preview="") | Q(preview_scene_only=False)
    )
    for template_id in studio.values_list("pk", flat=True):
        start_preview(template_id, actor=actor)
        drawn += 1
    ai = SceneTemplate.objects.filter(kind=enums.SceneTemplateKind.AI_SCENE, reference="").exclude(prompt="")
    for template_id in ai.values_list("pk", flat=True):
        if start_preview(template_id, actor=actor) is not None:
            queued += 1
    return drawn, queued


def _reference_content(picture: Image.Image, key: str) -> ContentFile[bytes]:
    data = compose.encode(picture.convert("RGB"), "WEBP", 88)
    return ContentFile(data, name=f"{key}-{hashlib.sha256(data).hexdigest()[:12]}.webp")


def _run_preview(job: SceneJob) -> SceneJob | None:
    row = get_settings()
    usage = pipeline.Usage()
    snapshot = dict(job.template_snapshot or {})
    try:
        # made from the words alone: the old reference (if any) is what is being replaced
        picture = pipeline.reference_scene(
            template_of(snapshot, with_reference=False), providers(row), config_of(row), usage
        )
    except pipeline.SceneError as exc:
        return fail(job, exc.code, usage=exc.usage or usage)
    except LLMError as exc:
        return fail(job, exc.code, usage=usage)
    except VaultError:
        return fail(job, "vault_unavailable", usage=usage)
    content = _reference_content(picture, str(snapshot.get("key") or "reference"))
    with transaction.atomic():
        locked = SceneJob.objects.select_for_update().filter(pk=job.pk).first()
        if locked is None or locked.state != enums.JobState.RUNNING:
            return None
        template = (
            SceneTemplate.objects.select_for_update().filter(pk=locked.template_id).first()
            if locked.template_id is not None
            else None
        )
        if template is not None and _same_scene(template, snapshot):  # not edited meanwhile
            store_reference(template, content, source=enums.SceneReferenceSource.GENERATED)
        instant = timeutils.now()
        locked.state = enums.JobState.SUCCEEDED
        locked.outcome = enums.SceneJobOutcome.ACCEPTED  # nothing to review: never expires
        locked.usage = _usage_dict(usage)
        locked.cost_usd = _cost(row, usage)
        locked.finished_at = instant
        locked.heartbeat_at = instant
        locked.reviewed_at = instant
        locked.save()
        return locked


# --- save a custom scene as a template ----------------------------------------------------------------------


def save_as_template(job_id: int, *, name: str, actor: Any, request: Any = None) -> SceneTemplate:
    """A scene staff typed becomes an AI template for everyone: its English description, named by them.
    Allowed while the scene waits for review or after it was accepted; once per job. The card shows the scene
    alone (no product): its reference is made from the description by a job, within the budget (until then
    the card shows the description's icon)."""
    clean = " ".join(name.split())[:TEMPLATE_NAME_MAX]
    if not clean:
        raise ValidationError({"name": [field_error("required", ERROR_MESSAGES["scene_template_name"])]})
    with transaction.atomic():
        job = SceneJob.objects.select_for_update().filter(pk=job_id).first()
        if job is None:
            raise NotFound
        snapshot = dict(job.template_snapshot or {})
        if (
            not snapshot.get("custom_text")
            or not snapshot.get("prompt")
            or job.state != enums.JobState.SUCCEEDED
        ):
            raise SceneNotCustom()
        key = f"{CUSTOM_KEY}-{job.pk}"
        if snapshot.get("saved_template_id") or SceneTemplate.objects.filter(key=key).exists():
            raise SceneTemplateExists()
        last = SceneTemplate.objects.order_by("-position").values_list("position", flat=True).first() or 0
        template = SceneTemplate.objects.create(
            key=key,
            kind=enums.SceneTemplateKind.AI_SCENE,
            name_uz=clean,
            name_ru=clean,
            name_en=clean,
            enabled=True,
            position=last + 1,
            prompt=str(snapshot["prompt"]),
            prompt_original=str(snapshot.get("custom_text") or ""),
            created_by=actor,
        )
        snapshot["saved_template_id"] = template.pk
        job.template_snapshot = snapshot
        job.save(update_fields=["template_snapshot", "updated_at"])
        audit.record(
            action="assistant.scene_template_created",
            entity_type="assistant_scene_template",
            entity_id=template.key,
            actor=actor,
            request=request,
            after={"name": clean, "from_job": job.pk, "prompt": {"length": len(template.prompt)}},
        )
    try:
        start_preview(template.pk, actor=actor)
    except SceneBudgetExceeded:
        logger.info("scene template %s: no reference made (budget)", template.key)
    return template


# --- review -------------------------------------------------------------------------------------------------


def _reviewable(job_id: int) -> SceneJob:
    job = SceneJob.objects.select_for_update().filter(pk=job_id).first()
    if job is None:
        raise NotFound
    if job.state != enums.JobState.SUCCEEDED or job.outcome != enums.SceneJobOutcome.PENDING:
        raise SceneNotReviewable()
    return job


def _delete_later(storage: Any, name: str) -> None:
    transaction.on_commit(lambda: storage.delete(name))


def _drop_files_on_commit(job: SceneJob) -> None:
    for field in (job.candidate, job.overlay):
        name = str(field.name or "")
        if name:
            _delete_later(field.storage, name)


def accept(
    job_id: int, *, make_main: bool, acknowledge_generated: bool, actor: Any, request: Any = None
) -> tuple[SceneJob, ProductImage]:
    """Accept a candidate: a scene becomes a new gallery image (the original stays); a fit writes the
    proposed renditions on the source image. ``scene_ack_required`` when part of the product was
    generated and staff did not tick that they checked it."""
    with transaction.atomic():
        job = _reviewable(job_id)
        if job.kind == enums.SceneJobKind.BANNER:  # a banner's image is used through use_for_banner
            raise SceneNotReviewable()
        image = ProductImage.objects.filter(pk=job.source_image_id).first() if job.source_image_id else None
        if image is None or image.sha256 != job.source_sha256 or job.product_id is None:
            raise SceneSourceChanged()
        if job.generated_inside_pct > 0 and not acknowledge_generated:
            raise ValidationError(
                {
                    "acknowledge_generated": [
                        field_error(
                            "required",
                            N_(
                                "Mahsulotning belgilangan qismi AI tomonidan chizilgan: "
                                "tekshirib, tasdiqlang."
                            ),
                        )
                    ]
                }
            )
        if job.kind == enums.SceneJobKind.FIT:
            accepted = catalog_services.set_image_renditions(
                job.product_id,
                image.pk,
                {ratio: crop for ratio, crop in job.crops.items()},
                actor=actor,
                request=request,
                mode=enums.RenditionMode.AUTO,
                product_box=job.product_box,
            )
        else:
            with job.candidate.storage.open(str(job.candidate.name), "rb") as handle:
                content = bytes(handle.read())
            accepted = catalog_services.add_generated_image(
                job.product_id,
                content,
                source=enums.ProductImageSource.AI_SCENE
                if job.kind == enums.SceneJobKind.AI_SCENE
                else enums.ProductImageSource.STUDIO,
                derived_from_id=image.pk,
                product_box=job.product_box,
                crops={ratio: tuple(crop) for ratio, crop in job.crops.items()},
                make_main=make_main,
                actor=actor,
                request=request,
            )
            _drop_files_on_commit(job)
        job.outcome = enums.SceneJobOutcome.ACCEPTED
        job.accepted_image = accepted
        job.reviewed_at = timeutils.now()
        job.reviewed_by = actor
        job.save(update_fields=["outcome", "accepted_image", "reviewed_at", "reviewed_by", "updated_at"])
        audit.record(
            action="assistant.scene_accepted",
            entity_type="product",
            entity_id=str(job.product_id),
            actor=actor,
            request=request,
            after={"job_id": job.pk, "kind": job.kind, "image_id": accepted.pk, "source_image_id": image.pk},
        )
    return job, accepted


def discard(job_id: int, *, actor: Any, request: Any = None) -> SceneJob:
    with transaction.atomic():
        job = _reviewable(job_id)
        job.outcome = enums.SceneJobOutcome.DISCARDED
        job.reviewed_at = timeutils.now()
        job.reviewed_by = actor
        job.save(update_fields=["outcome", "reviewed_at", "reviewed_by", "updated_at"])
        _drop_files_on_commit(job)
    return job


# --- housekeeping -------------------------------------------------------------------------------------------


def reap_stale(*, now: Any = None) -> list[int]:
    instant = now or timeutils.now()
    cutoff = instant - STALE_AFTER
    candidates = SceneJob.objects.filter(
        Q(state=enums.JobState.RUNNING, heartbeat_at__lt=cutoff)
        | Q(state=enums.JobState.QUEUED, created_at__lt=instant - QUEUED_STALE_AFTER)
    ).values_list("pk", flat=True)
    reaped: list[int] = []
    for job_id in list(candidates):
        with transaction.atomic():
            job = SceneJob.objects.select_for_update().filter(pk=job_id).first()
            if job is None or job.state not in ACTIVE_SCENE_STATES:
                continue
            job.state = enums.JobState.FAILED
            job.error_code = WORKER_LOST
            job.finished_at = instant
            job.save(update_fields=["state", "error_code", "finished_at", "updated_at"])
            reaped.append(job.pk)
    return reaped


def expire_candidates(*, now: Any = None) -> int:
    """Unreviewed candidates past ``expires_at`` become ``expired`` and their files are removed."""
    instant = now or timeutils.now()
    count = 0
    for job_id in SceneJob.objects.filter(
        state=enums.JobState.SUCCEEDED, outcome=enums.SceneJobOutcome.PENDING, expires_at__lt=instant
    ).values_list("pk", flat=True):
        with transaction.atomic():
            job = SceneJob.objects.select_for_update().filter(pk=job_id).first()
            if job is None or job.outcome != enums.SceneJobOutcome.PENDING:
                continue
            job.outcome = enums.SceneJobOutcome.EXPIRED
            job.save(update_fields=["outcome", "updated_at"])
            _drop_files_on_commit(job)
            count += 1
    return count
