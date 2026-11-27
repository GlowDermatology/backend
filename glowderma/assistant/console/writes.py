"""The console's writes (superusers): assistant settings, provider parameters, prompt versions.

Every write locks its row, saves only what changed and records an ``assistant.*`` audit row in the same
transaction. Prompt instructions are audited as their length and SHA-256 digest (not the text). The API key is
not here: :mod:`glowderma.assistant.credentials`.

Prompt version lifecycle (one template per purpose):

* :func:`new_draft` — a draft copied from the active version (or the open draft, if there is one);
* :func:`edit_draft` — only a draft or a tested version is edited; an edit makes it a draft again;
* :func:`start_test` — a test job on a sample product (``services.start_prompt_test``, the generation
  service); :func:`refresh_test_status` marks the version ``tested`` once that job succeeded;
* :func:`activate` — a tested version, or (rollback) a retired one, becomes the active version; the previous
  active one is retired. The next job reads it: no restart.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from django.db import transaction
from django.db.models import Max

from glowderma.assistant.models import (
    SETTINGS_PK,
    AssistantSettings,
    GenerationJob,
    PromptTemplate,
    PromptVersion,
    ProviderConfig,
    SceneSettings,
    SceneTemplate,
)
from glowderma.core import audit, enums, timeutils

Status = enums.PromptVersionStatus

SETTINGS_FIELDS = (
    "enabled",
    "active_provider",
    "daily_budget_usd",
    "monthly_budget_usd",
    "per_user_daily_jobs",
    "max_images",
)
PROVIDER_FIELDS = (
    "enabled",
    "model_id",
    "temperature",
    "max_output_tokens",
    "timeout_seconds",
    "max_retries",
    "price_input_per_mtok_usd",
    "price_output_per_mtok_usd",
)
SCENE_SETTINGS_FIELDS = (
    "enabled",
    "ai_enabled",
    "scene_provider",
    "segmenter",
    "analyst_model",
    "image_model",
    "image_size",
    "bria_model",
    "qa_check",
    "max_occluded_pct",
    "max_attempts",
    "daily_budget_usd",
    "monthly_budget_usd",
    "per_user_daily_jobs",
    "candidate_ttl_hours",
    "price_text_input",
    "price_text_output",
    "price_image_input",
    "price_image_text_output",
    "price_image_output",
    "price_bria_call",
    "price_bria_scene_call",
    "style",
)
SCENE_TEMPLATE_FIELDS = ("name_uz", "name_ru", "name_en", "enabled", "position", "params", "prompt")
EDITABLE_STATUSES = (Status.DRAFT, Status.TESTED)
ACTIVATABLE_STATUSES = (Status.TESTED, Status.RETIRED)


class PromptRefused(Exception):
    """A prompt version operation that does not apply to the version's state; ``code`` names why."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _plain(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    return str(value)


def _apply(
    row: Any, fields: tuple[str, ...], changes: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    for name in fields:
        if name in changes and getattr(row, name) != changes[name]:
            before[name] = _plain(getattr(row, name))
            setattr(row, name, changes[name])
            after[name] = _plain(changes[name])
    return before, after


def update_settings(changes: Mapping[str, Any], *, actor: Any, request: Any = None) -> AssistantSettings:
    with transaction.atomic():
        row = AssistantSettings.objects.select_for_update().get(pk=SETTINGS_PK)
        before, after = _apply(row, SETTINGS_FIELDS, changes)
        if after:
            row.updated_by = actor
            row.save()
            audit.record(
                action="assistant.settings_updated",
                entity_type="assistant_settings",
                entity_id=str(SETTINGS_PK),
                actor=actor,
                request=request,
                before=before,
                after=after,
            )
    return row


def update_provider(
    provider: str, changes: Mapping[str, Any], *, actor: Any, request: Any = None
) -> ProviderConfig:
    with transaction.atomic():
        row = ProviderConfig.objects.select_for_update().get(provider=provider)
        before, after = _apply(row, PROVIDER_FIELDS, changes)
        if after:
            row.updated_by = actor
            row.save()
            audit.record(
                action="assistant.provider_updated",
                entity_type="assistant_provider",
                entity_id=provider,
                actor=actor,
                request=request,
                before=before,
                after=after,
            )
    return row


def update_scene_settings(changes: Mapping[str, Any], *, actor: Any, request: Any = None) -> SceneSettings:
    with transaction.atomic():
        row = SceneSettings.objects.select_for_update().get(pk=SETTINGS_PK)
        before, after = _apply(row, SCENE_SETTINGS_FIELDS, changes)
        if "style" in after:  # audited as length and digest, like prompt instructions
            before["style"], after["style"] = digest(str(before["style"] or "")), digest(str(row.style))
        if after:
            row.updated_by = actor
            row.save()
            audit.record(
                action="assistant.scene_settings_updated",
                entity_type="assistant_scene_settings",
                entity_id=str(SETTINGS_PK),
                actor=actor,
                request=request,
                before=before,
                after=after,
            )
    return row


def update_scene_template(
    template_id: int, changes: Mapping[str, Any], *, actor: Any, request: Any = None
) -> SceneTemplate:
    with transaction.atomic():
        row = SceneTemplate.objects.select_for_update().get(pk=template_id)
        before, after = _apply(row, SCENE_TEMPLATE_FIELDS, changes)
        if after:
            row.save()
            if {"params", "prompt"} & set(after):
                from glowderma.assistant.scenes import services as scene_services

                scene_services.template_changed(row)  # the old sample picture shows another scene
            audit.record(
                action="assistant.scene_template_updated",
                entity_type="assistant_scene_template",
                entity_id=row.key,
                actor=actor,
                request=request,
                before=before,
                after=after,
            )
    return row


# --- prompt versions ----------------------------------------------------------------------------------------


def digest(text: str) -> dict[str, Any]:
    return {"length": len(text), "sha256": hashlib.sha256(text.encode()).hexdigest()}


def _record(action: str, version: PromptVersion, *, actor: Any, request: Any, **after: Any) -> None:
    audit.record(
        action=action,
        entity_type="assistant_prompt",
        entity_id=f"{version.template.purpose}:{version.number}",
        actor=actor,
        request=request,
        after={"purpose": version.template.purpose, "number": version.number, **after},
    )


def new_draft(purpose: str, *, actor: Any, request: Any = None) -> PromptVersion:
    """The template's open draft, or a new one copied from the active (else the latest) version."""
    with transaction.atomic():
        template = PromptTemplate.objects.select_for_update().get(purpose=purpose)
        versions = PromptVersion.objects.filter(template=template)
        open_draft = versions.filter(status=Status.DRAFT).order_by("-number").first()
        if open_draft is not None:
            return open_draft
        source = versions.filter(status=Status.ACTIVE).first() or versions.order_by("-number").first()
        number = (versions.aggregate(top=Max("number"))["top"] or 0) + 1
        draft = PromptVersion.objects.create(
            template=template,
            number=number,
            instructions=source.instructions if source is not None else "-",
            status=Status.DRAFT,
            created_by=actor,
        )
        _record(
            "assistant.prompt_draft_created",
            draft,
            actor=actor,
            request=request,
            source=source.number if source is not None else None,
            instructions=digest(draft.instructions),
        )
    return draft


def edit_draft(version_id: int, instructions: str, *, actor: Any, request: Any = None) -> PromptVersion:
    with transaction.atomic():
        version = PromptVersion.objects.select_for_update().select_related("template").get(pk=version_id)
        if version.status not in EDITABLE_STATUSES:
            raise PromptRefused("not_a_draft")
        if version.instructions == instructions:
            return version
        version.instructions = instructions
        version.status = Status.DRAFT
        version.tested_at = None
        version.last_test = {}
        version.save()
        _record(
            "assistant.prompt_draft_updated",
            version,
            actor=actor,
            request=request,
            instructions=digest(instructions),
        )
    return version


def start_test(version_id: int, product_id: int, *, actor: Any, request: Any = None) -> GenerationJob:
    """Enqueue a test generation of ``version`` on a sample product (the generation service runs it)."""
    from glowderma.assistant import services  # the generation service (sibling file, same app)

    version = PromptVersion.objects.select_related("template").get(pk=version_id)
    if version.status not in EDITABLE_STATUSES:
        raise PromptRefused("not_a_draft")
    job: GenerationJob = services.start_prompt_test(
        version=version, product_id=product_id, actor=actor, request=request
    )
    with transaction.atomic():
        version = PromptVersion.objects.select_for_update().select_related("template").get(pk=version_id)
        version.last_test = {
            "job_id": job.pk,
            "product_id": product_id,
            "at": timeutils.isoformat(timeutils.now()),
            "by": getattr(actor, "pk", None),
            "state": job.state,
        }
        version.save(update_fields=["last_test", "updated_at"])
        _record(
            "assistant.prompt_test_started",
            version,
            actor=actor,
            request=request,
            job_id=job.pk,
            product_id=product_id,
        )
    return job


def refresh_test_status(version: PromptVersion) -> PromptVersion:
    """A draft whose last test job has finished: record the outcome; ``tested`` when it succeeded."""
    job_id = (version.last_test or {}).get("job_id")
    if version.status != Status.DRAFT or not job_id:
        return version
    job = GenerationJob.objects.filter(pk=job_id).first()
    if job is None or job.state not in (enums.JobState.SUCCEEDED, enums.JobState.FAILED):
        return version
    with transaction.atomic():
        locked = PromptVersion.objects.select_for_update().get(pk=version.pk)
        if locked.status != Status.DRAFT or (locked.last_test or {}).get("job_id") != job_id:
            return locked
        locked.last_test = {
            **locked.last_test,
            "state": job.state,
            "error_code": job.error_code,
            "suggestions": len(job.suggestions or []),
            "warnings": len(job.warnings or []),
            "cost_usd": str(job.cost_usd),
        }
        if job.state == enums.JobState.SUCCEEDED:
            locked.status = Status.TESTED
            locked.tested_at = job.finished_at or timeutils.now()
        locked.save()
    return locked


def activate(version_id: int, *, actor: Any, request: Any = None) -> PromptVersion:
    """Make ``version`` the template's active version: a tested one, or a retired one (rollback)."""
    with transaction.atomic():
        current = PromptVersion.objects.select_related("template").get(pk=version_id)
        template = PromptTemplate.objects.select_for_update().get(pk=current.template_id)
        version = PromptVersion.objects.select_for_update().select_related("template").get(pk=version_id)
        if version.status not in ACTIVATABLE_STATUSES:
            raise PromptRefused("not_tested")
        rollback = version.status == Status.RETIRED
        previous = (
            PromptVersion.objects.select_for_update().filter(template=template, status=Status.ACTIVE).first()
        )
        if previous is not None:
            previous.status = Status.RETIRED
            previous.save(update_fields=["status", "updated_at"])
        version.status = Status.ACTIVE
        version.activated_at = timeutils.now()
        version.activated_by = actor
        version.save(update_fields=["status", "activated_at", "activated_by", "updated_at"])
        _record(
            "assistant.prompt_rolled_back" if rollback else "assistant.prompt_activated",
            version,
            actor=actor,
            request=request,
            previous=previous.number if previous is not None else None,
        )
    return version
