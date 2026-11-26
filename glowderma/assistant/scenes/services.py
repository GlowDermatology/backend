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

import logging
from dataclasses import dataclass
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
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
from glowderma.assistant.scenes import pipeline
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

STALE_AFTER = timedelta(minutes=6)  # longer than the task's hard limit (tasks.SCENE_TIME_LIMIT)
FAKE_KEY = "fake-key"
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


class SceneNotReviewable(Conflict):
    default_code = "scene_job_not_ready"
    default_detail = N_("Bu natija hali tayyor emas yoki allaqachon ko‘rib chiqilgan.")


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
    }


def start_job(
    product_id: int, image_id: int, *, kind: str, template_id: int | None, actor: Any
) -> StartResult:
    """Queue a scene / fit job on a saved image, or return the image's active job (``created=False``)."""
    row = get_settings()
    if not row.enabled:
        raise ScenesDisabled()
    image = ProductImage.objects.filter(pk=image_id, product_id=product_id).first()
    if image is None:
        raise NotFound
    template: SceneTemplate | None = None
    if kind != enums.SceneJobKind.FIT:
        template = SceneTemplate.objects.filter(pk=template_id, enabled=True).first() if template_id else None
        if template is None or template.kind != kind:
            raise ValidationError(
                {"template_id": [field_error("invalid", ERROR_MESSAGES["scene_template_unavailable"])]}
            )
        if kind == enums.SceneJobKind.AI_SCENE and not row.ai_enabled:
            raise AiScenesDisabled()
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
                template_snapshot=_template_snapshot(template) if template is not None else {},
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


def providers(row: SceneSettings) -> pipeline.Providers:
    gemini = _key(enums.AssistantProvider.GEMINI)
    cutter_key = _key(enums.AssistantProvider.BRIA) if row.segmenter == enums.SceneSegmenter.BRIA else gemini
    return pipeline.Providers(
        segmenter=segmentation_registry.get_segmenter(
            row.segmenter, image_model=row.image_model, cutter_key=cutter_key, bria_model=row.bria_model
        ),
        image_model=llm_registry.get_image_model(),
        content_model=llm_registry.get_content_model(),
        api_key=gemini,
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


def template_of(snapshot: dict[str, Any]) -> Template:
    return Template(
        key=str(snapshot.get("key") or ""),
        kind=snapshot.get("kind") or "studio",
        name=dict(snapshot.get("name") or {}),
        params=dict(snapshot.get("params") or {}),
        prompt=str(snapshot.get("prompt") or ""),
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
        template = template_of(job.template_snapshot)
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
        | Q(state=enums.JobState.QUEUED, created_at__lt=cutoff)
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
