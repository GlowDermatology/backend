"""Banner images (contract §2.20.8): queue a banner image job, make it in the worker, use it for a banner.

A banner job is a :class:`SceneJob` of kind ``banner`` with no source image: its ``inputs`` name the mode
(``products``: 1-3 catalog products, each from its main photo; ``scene``: a scene alone) and, per product, the
photo used and its SHA-256 (a photo replaced meanwhile fails the job, as a product scene does). It shares the
scenes' switches, budgets, daily limit, worker queue (one scene at a time) and review window. The image is
made by :mod:`.banner`; staff then use it for a new or an existing banner (:func:`use_for_banner`), which
stores it through the banner upload path (the same checks and the 1000 px copy the app loads).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from django.core.files.base import ContentFile
from django.db import transaction
from rest_framework.exceptions import NotFound, ValidationError

from glowderma.assistant import topics
from glowderma.assistant.models import ACTIVE_SCENE_STATES, SceneJob, SceneTemplate
from glowderma.assistant.scenes import banner, pipeline, prompts
from glowderma.assistant.scenes import services as scenes
from glowderma.catalog import services as catalog_services
from glowderma.catalog.models import Banner, Product, ProductImage
from glowderma.core import audit, compose, enums, outbox, timeutils
from glowderma.core.api.exceptions import field_error
from glowderma.integrations.llm.ports import LLMError
from glowderma.integrations.vault.ports import VaultError

logger = logging.getLogger(__name__)

MODE_PRODUCTS = "products"
MODE_SCENE = "scene"
MODES = (MODE_PRODUCTS, MODE_SCENE)
SOURCE_CHANGED = "scene_source_changed"


@dataclass(frozen=True, slots=True)
class StartResult:
    job: SceneJob
    created: bool


def _invalid(field: str, code: str) -> ValidationError:
    return ValidationError({field: [field_error("invalid", scenes.message_of(code))]})


def _products(product_ids: list[int]) -> list[dict[str, Any]]:
    """Each product's main photo (the first by position), in the order staff picked them."""
    if not 1 <= len(product_ids) <= banner.MAX_PRODUCTS or len(set(product_ids)) != len(product_ids):
        raise _invalid("product_ids", "banner_products")
    rows = {product.pk: product for product in Product.objects.filter(pk__in=product_ids)}
    picked = []
    for product_id in product_ids:
        product = rows.get(product_id)
        if product is None:
            raise _invalid("product_ids", "banner_products")
        image = ProductImage.objects.filter(product_id=product_id).order_by("position", "id").first()
        name = product.name_uz or product.billz_name or f"#{product_id}"
        if image is None:
            message = f"{name}: {scenes.message_of(banner.PRODUCT_PHOTO_MISSING)}"
            raise ValidationError({"product_ids": [field_error("invalid", message)]})
        picked.append({"id": product_id, "image_id": image.pk, "sha256": image.sha256, "name": name})
    return picked


def _active(actor: Any) -> SceneJob | None:
    return SceneJob.objects.filter(
        kind=enums.SceneJobKind.BANNER, created_by=actor, state__in=ACTIVE_SCENE_STATES
    ).first()


def start(
    *, mode: str, product_ids: list[int], template_id: int | None, custom_prompt: str, actor: Any
) -> StartResult:
    """Queue a banner image, or return the one this staff member is still waiting for (``created=False``)."""
    row = scenes.get_settings()
    if not row.enabled:
        raise scenes.ScenesDisabled()
    if not row.ai_enabled:
        raise scenes.AiScenesDisabled()
    if mode not in MODES:
        raise ValidationError({"mode": [field_error("invalid")]})
    custom = " ".join(custom_prompt.split())
    template: SceneTemplate | None = None
    if custom:
        if len(custom) > prompts.CUSTOM_MAX_CHARS:
            raise _invalid("custom_prompt", "scene_custom_text")
        snapshot = scenes._custom_snapshot(custom)
    else:
        template = SceneTemplate.objects.filter(pk=template_id, enabled=True).first() if template_id else None
        if template is None or template.kind != enums.SceneTemplateKind.AI_SCENE:
            raise _invalid("template_id", "scene_template_unavailable")
        snapshot = scenes._template_snapshot(template)
    products = _products(product_ids) if mode == MODE_PRODUCTS else []
    if mode == MODE_SCENE and product_ids:
        raise _invalid("product_ids", "banner_products")
    active = _active(actor)
    if active is not None:
        return StartResult(job=active, created=False)
    if scenes.jobs_today(actor) >= row.per_user_daily_jobs:
        raise scenes.SceneDailyLimit()
    if scenes.spent_today() >= row.daily_budget_usd or scenes.spent_this_month() >= row.monthly_budget_usd:
        raise scenes.SceneBudgetExceeded()
    with transaction.atomic():
        job = SceneJob.objects.create(
            source_sha256="",
            kind=enums.SceneJobKind.BANNER,
            template=template,
            template_snapshot=snapshot,
            inputs={"mode": mode, "products": products},
            created_by=actor,
        )
        outbox.enqueue(topics.SCENE_QUEUED, {"job_id": job.pk}, dedupe_key=f"{topics.SCENE_QUEUED}:{job.pk}")
    logger.info("banner job %s queued", job.pk)
    return StartResult(job=job, created=True)


# --- run (worker) -------------------------------------------------------------------------------------------


def _beat(job: SceneJob) -> None:
    """A product cut out: the job is alive (three products take a while)."""
    SceneJob.objects.filter(pk=job.pk, state=enums.JobState.RUNNING).update(heartbeat_at=timeutils.now())


def _cutouts(
    job: SceneJob, providers: pipeline.Providers, config: pipeline.Config, usage: pipeline.Usage
) -> list[banner.Cutout]:
    cutouts = []
    for item in list((job.inputs or {}).get("products") or []):
        image = ProductImage.objects.filter(pk=item.get("image_id"), product_id=item.get("id")).first()
        if image is None or image.sha256 != item.get("sha256"):
            raise pipeline.SceneError(SOURCE_CHANGED, f"product {item.get('id')}: photo changed", usage=usage)
        photo = pipeline.load_photo(scenes._source_bytes(image))
        try:
            analysis = pipeline.analyse(photo, providers, config, usage)
            cutouts.append(banner.cutout_of(int(item["id"]), analysis, config))
        except pipeline.SceneError as exc:
            inputs = dict(job.inputs or {})
            inputs["failed_product_id"] = item.get("id")
            inputs["failed_reason"] = exc.code
            SceneJob.objects.filter(pk=job.pk).update(inputs=inputs)
            raise pipeline.SceneError(
                banner.PRODUCT_PHOTO_UNUSABLE, str(exc), usage=exc.usage or usage
            ) from exc
        _beat(job)
    return cutouts


def _crops(size: tuple[int, int]) -> dict[str, list[float]]:
    width, height = size
    web = banner.web_window(size)
    return {
        "2.15:1": [0.0, 0.0, 1.0, 1.0],
        "2:1": [round(web[0] / width, 5), 0.0, round((web[2] - web[0]) / width, 5), 1.0],
    }


def run(job: SceneJob) -> SceneJob | None:
    """The worker side of a claimed banner job."""
    row = scenes.get_settings()
    usage = pipeline.Usage()
    try:
        used = scenes.providers(row)
        config = scenes.config_of(row)
        snapshot = dict(job.template_snapshot or {})
        if snapshot.get("custom_text") and not snapshot.get("prompt"):
            snapshot["prompt"] = pipeline.custom_scene(str(snapshot["custom_text"]), used, config, usage)
            SceneJob.objects.filter(pk=job.pk).update(template_snapshot=snapshot)
        template = scenes.template_of(snapshot)
        if (job.inputs or {}).get("mode") == MODE_PRODUCTS:
            result = banner.with_products(_cutouts(job, used, config, usage), template, used, config, usage)
        else:
            result = banner.scene_only(template, used, config, usage)
    except pipeline.SceneError as exc:
        return scenes.fail(job, exc.code, usage=exc.usage or usage)
    except LLMError as exc:
        return scenes.fail(job, exc.code, usage=getattr(exc, "usage", None) or usage)
    except VaultError:
        return scenes.fail(job, "vault_unavailable", usage=usage)
    width, height = result.final.size
    candidate = ContentFile(compose.encode(result.final, "WEBP", 92), name=f"{job.pk}-banner.webp")
    with transaction.atomic():
        locked = SceneJob.objects.select_for_update().filter(pk=job.pk).first()
        if locked is None or locked.state != enums.JobState.RUNNING:
            return None
        locked.candidate.save(candidate.name or "banner.webp", candidate, save=False)
        locked.product_box = None
        locked.crops = _crops((width, height))
        locked.warnings = result.warnings
        locked.usage = scenes._usage_dict(usage)
        locked.cost_usd = scenes._cost(row, usage)
        locked.state = enums.JobState.SUCCEEDED
        instant = timeutils.now()
        locked.finished_at = instant
        locked.heartbeat_at = instant
        locked.expires_at = instant + timedelta(hours=row.candidate_ttl_hours)
        locked.save()
        return locked


# --- use ----------------------------------------------------------------------------------------------------


def use_for_banner(
    job_id: int,
    data: catalog_services.BannerInput,
    *,
    banner_id: int | None,
    actor: Any,
    request: Any = None,
) -> tuple[SceneJob, Banner]:
    """The image becomes a new banner's (``banner_id`` ``None``: ``data`` is the whole banner) or an existing
    banner's (``data`` holds the fields staff changed), in one transaction with the banner's other fields."""
    with transaction.atomic():
        job = scenes._reviewable(job_id)
        if job.kind != enums.SceneJobKind.BANNER:
            raise scenes.SceneNotReviewable()
        if not job.candidate:
            raise scenes.SceneNotReviewable()
        with job.candidate.storage.open(str(job.candidate.name), "rb") as handle:
            content = bytes(handle.read())
        if banner_id is None:
            saved = catalog_services.create_banner(data, content, actor=actor, request=request)
        else:
            existing = Banner.objects.filter(pk=banner_id).first()
            if existing is None:
                raise NotFound
            saved = catalog_services.update_banner(existing, data, content, actor=actor, request=request)
        job.outcome = enums.SceneJobOutcome.ACCEPTED
        job.accepted_banner = saved
        job.reviewed_at = timeutils.now()
        job.reviewed_by = actor
        job.save(update_fields=["outcome", "accepted_banner", "reviewed_at", "reviewed_by", "updated_at"])
        scenes._drop_files_on_commit(job)
        audit.record(
            action="assistant.banner_image_used",
            entity_type="banner",
            entity_id=str(saved.pk),
            actor=actor,
            request=request,
            after={"job_id": job.pk, "mode": (job.inputs or {}).get("mode"), "new": banner_id is None},
        )
    return job, saved
